import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from comfy_split.media_server import BLOCK, MediaServer
from comfy_split.production import outputs


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output, self.data = self.root / "output", self.root / "data"
        self.output.mkdir()
        (self.data / "jobs").mkdir(parents=True)
        self.reload = AsyncMock()
        self.server = MediaServer(self.output, self.data,
                                 [SimpleNamespace(reload=SimpleNamespace(aio=self.reload))])
        self.content = b"RIFFxxxxWAVE" + bytes(range(256)) * 2050
        (self.output / "音 楽.wav").write_bytes(self.content)
        self.id = str(uuid.uuid4())
        self.job = dict(status="completed", history=dict(status=dict(status_str="success", completed=True),
            outputs={"4": {"audio": [dict(filename="音 楽.wav", subfolder="", type="output"),
                                    dict(filename="bad.wav", subfolder="../private", type="output")]}}))
        (self.data / "jobs" / (self.id + ".json")).write_text(json.dumps(self.job))

    async def request(self, path="/view", query="type=output&filename=音 楽.wav", headers=(), method="GET", hook=None):
        events = []
        queue = asyncio.Queue()
        queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})

        async def send(event):
            events.append(event)
            if hook:
                await hook(event)

        await self.server(dict(type="http", method=method, path=path,
            query_string=query.encode(), headers=list(headers)), queue.get, send)
        start = events[0]
        self.assertTrue(not events[-1].get("more_body"))
        return start["status"], dict(start["headers"]), b"".join(e.get("body", b"") for e in events), events

    async def test_full_head_ranges_seek_and_etag(self):
        code, headers, body, events = await self.request()
        self.assertEqual(code, 200)
        self.assertEqual(body, self.content)
        self.assertLessEqual(max(len(e.get("body", b"")) for e in events), BLOCK)
        self.assertEqual(headers[b"content-type"], b"audio/wav")
        code, h, body, _ = await self.request(method="HEAD")
        self.assertEqual((code, body, h[b"content-length"]), (200, b"", str(len(self.content)).encode()))
        for value, first, last in [("bytes=10-20", 10, 21), ("bytes=-7", len(self.content)-7, len(self.content)),
                                   ("bytes=524800-", 524800, len(self.content))]:
            code, h, body, _ = await self.request(headers=[(b"range", value.encode())])
            self.assertEqual(code, 206)
            self.assertEqual(body, self.content[first:last])
            self.assertEqual(h[b"content-range"], f"bytes {first}-{last-1}/{len(self.content)}".encode())
        for value in ["bytes=999999-", "bytes=5-3", "bytes=-0", "bytes=0-1,5-8", "garbage"]:
            code, h, _, _ = await self.request(headers=[(b"range", value.encode())])
            self.assertEqual(code, 416)
            self.assertEqual(h[b"content-range"], f"bytes */{len(self.content)}".encode())
        code, _, body, _ = await self.request(headers=[(b"if-none-match", headers[b"etag"])])
        self.assertEqual((code, body), (304, b""))
        code, _, body, _ = await self.request(headers=[(b"range", b"bytes=1-3"), (b"if-range", b'"old"')])
        self.assertEqual((code, body), (200, self.content))

    async def test_read_only_paths_and_symlinks(self):
        (self.root / "private.wav").write_text("secret")
        (self.output / "link.wav").symlink_to(self.root / "private.wav")
        (self.output / "linked").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.output / "fifo.wav")
        for query in ["filename=../private.wav", "filename=private.wav&subfolder=..", "filename=link.wav",
                      "filename=private.wav&subfolder=linked", "filename=fifo.wav", "filename=.secret.wav",
                      "filename=x.html", "filename=x.wav&type=input", "filename=x.wav&preview=webp",
                      "filename=x.wav&filename=private.wav", "filename=x.wav&subfolder=a//b"]:
            code, _, body, _ = await self.request(query=query)
            self.assertIn(code, (400, 404))
            self.assertNotIn(b"secret", body)
        for path in ["/prompt", "/split/startup", "/object_info", "/view/other"]:
            self.assertEqual((await self.request(path=path))[0], 404)
        self.reload.reset_mock()
        self.assertEqual((await self.request(method="POST"))[0], 405)
        self.reload.assert_not_called()

    async def test_saved_metadata_is_minimal_and_indexes_match_existing_tools(self):
        code, _, body, _ = await self.request(path="/history/" + self.id, query="")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[self.id], self.job["history"])
        code, _, body, _ = await self.request(path="/outputs/" + self.id, query="limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["items"], outputs(self.job))
        self.assertEqual((await self.request(path="/history/../controller", query=""))[0], 400)
        (self.data / "state").mkdir()
        (self.data / "state/controller.json").write_text(json.dumps({"jobs": {self.id: self.job}}))
        (self.data / "jobs" / (self.id + ".json")).unlink()
        self.assertEqual((await self.request(path="/history/" + self.id, query=""))[0], 200)
        self.assertEqual((await self.request(path="/history/" + str(uuid.uuid4()), query=""))[0], 404)

    async def test_refresh_failure_never_serves_a_stale_file(self):
        self.reload.side_effect = RuntimeError("storage unavailable")
        code, h, body, _ = await self.request()
        self.assertEqual((code, h[b"retry-after"]), (503, b"2"))
        self.assertNotIn(self.content[:12], body)

    async def test_another_request_can_reload_between_streamed_chunks(self):
        interleaved = False

        async def hook(event):
            nonlocal interleaved
            if event.get("more_body") and not interleaved:
                interleaved = True
                code, _, part, _ = await self.request(headers=[(b"range", b"bytes=0-11")])
                self.assertEqual((code, part), (206, self.content[:12]))

        code, _, body, _ = await self.request(hook=hook)
        self.assertEqual((code, body, interleaved), (200, self.content, True))

    async def test_replaced_file_does_not_mix_versions(self):
        async def hook(event):
            if event.get("more_body"):
                (self.output / "音 楽.wav").write_bytes(b"x" * len(self.content))
        with self.assertRaisesRegex(OSError, "changed during delivery"):
            await self.request(hook=hook)

    async def test_volume_remount_inode_change_keeps_stream_and_etag(self):
        original_fstat = os.fstat
        inode = 0

        def remounted(fd):
            nonlocal inode
            inode += 1
            info = original_fstat(fd)
            return SimpleNamespace(st_ino=inode, st_mode=info.st_mode, st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)

        with patch("comfy_split.media_server.os.fstat", side_effect=remounted):
            code, h, body, _ = await self.request()
            self.assertEqual((code, body), (200, self.content))
            _, other, _, _ = await self.request(method="HEAD")
            self.assertEqual(h[b"etag"], other[b"etag"])


if __name__ == "__main__":
    unittest.main()
