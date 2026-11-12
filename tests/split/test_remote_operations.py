import asyncio
import tempfile
import threading
import subprocess
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiohttp import ClientConnectionError, web
from PIL import Image

from comfy_split import gateway, model_files as models, production as media, runtime, split_admin as admin
from comfy_split.gateway import Controller
from comfy_split.storage import MOUNTS
from tests.split.support import volume_mocks


class RemoteOperations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.dirs = {key: root / key for key in ("input", "output", "models")}
        for p in self.dirs.values():
            p.mkdir()
        patcher = patch.dict(MOUNTS, self.dirs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.c = Controller(None, None, None, volume_mocks(), root / "state", extensions=())
        self.c.gpu_status = AsyncMock(return_value={"containers": 0})

    async def run_op(self, name, **args):
        return await admin.handle(self.c, name, args)

    async def test_failed_candidate_discard_survives_unreachable_manager_and_archive_failure(self):
        """A failed candidate must remain discardable when both Manager and archiving fail."""
        c = self.c
        c.journal.data["candidate"] = {"version": "env-failed", "status": "failed"}
        child = SimpleNamespace(pid=123, returncode=None, wait=AsyncMock(return_value=0))
        c.candidate.process = child
        with patch.object(admin, "manager", AsyncMock(side_effect=ClientConnectionError("unreachable"))) as manager, \
                patch.object(c.candidate, "archive_temp", side_effect=OSError("archive failed")), \
                patch.object(runtime.os, "killpg") as kill, \
                self.assertLogs("comfy_split.runtime", level="ERROR"):
            result = await self.run_op("discard_environment", candidate="env-failed", request_id="discard-failed")
            self.assertEqual(result["status"], "completed")
        manager.assert_not_awaited()
        kill.assert_called_once_with(123, signal.SIGTERM)
        child.wait.assert_awaited_once()
        self.assertIsNone(c.candidate.process)
        self.assertIsNone(c.journal.data["candidate"])
        self.assertEqual(c.journal.data["environment"], "base")
        self.assertEqual(await self.run_op("discard_environment", candidate="env-failed",
            request_id="discard-failed"), result)

    async def test_failed_reservation_commit_does_not_leave_a_candidate_without_a_task(self):
        """An uncommitted reservation must not block later work or start a preparation task."""
        self.c.volumes["data"].commit.aio.side_effect = [None, OSError("commit failed"), None]
        result = await self.run_op("prepare_environment", active="base", request_id="failed-reservation")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(self.c.journal.data["candidate"])
        self.assertIsNone(self.c.prepare_task)
        self.assertFalse(self.c.background_work())
        self.c.journal.assert_idle()

    async def test_browser_relay_failure_does_not_fail_preparation_or_block_other_relays(self):
        """One failed browser relay must leave the candidate editable and allow reconnection."""
        socket = AsyncMock()
        socket.__aiter__.return_value = []
        self.c.sockets = {"first": set(), "second": set()}
        self.c.client = SimpleNamespace(ws_connect=AsyncMock(
            side_effect=[ClientConnectionError("relay disconnected"), socket, socket]))
        self.c.candidate.start = AsyncMock()
        with patch.object(gateway, "create_environment", return_value="copy"):
            await self.run_op("prepare_environment", active="base", request_id="with-browsers")
            with self.assertLogs("comfy_split.gateway", level="WARNING"):
                await self.c.prepare_task
        result = await self.run_op("get_admin_operation", request_id="with-browsers")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.c.journal.data["candidate"]["status"], "editing")
        self.assertNotIn("first", self.c.candidate_relays)
        self.assertIn("second", self.c.candidate_relays)
        await self.c.attach_candidate("first")
        self.assertIn("first", self.c.candidate_relays)
        await self.c.close_candidate_relays()

    async def test_slow_candidate_copy_returns_receipt_without_blocking_reads_or_model_import(self):
        """Slow preparation must preserve responsive management reads and idempotent receipts."""
        started, release = asyncio.Event(), threading.Event()
        booting, ready = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def copy(source):
            self.assertEqual(source, "base")
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test did not release candidate copy")
            return "copy"

        async def boot(*args, **kwargs):
            self.c.candidate.process = SimpleNamespace(returncode=None)
            booting.set()
            await ready.wait()

        self.c.candidate.start = AsyncMock(side_effect=boot)
        with patch.object(gateway, "create_environment", side_effect=copy) as clone, \
                patch.object(admin, "start_download", AsyncMock(return_value="download")):
            args = dict(active="base", request_id="prepare")
            first = await asyncio.wait_for(self.run_op("prepare_environment", **args), 1)
            self.assertEqual(first["status"], "running")
            task = self.c.prepare_task
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertTrue(self.c.background_work())
                status = await asyncio.wait_for(self.run_op("get_split_status"), 1)
                self.assertEqual(status["candidate"], {"version": None, "status": "creating"})
                self.assertEqual(await self.run_op("prepare_environment", **args), first)
                self.assertEqual(await self.run_op("get_admin_operation", request_id="prepare"), first)
                added = await asyncio.wait_for(self.run_op("add_model", request_id="add",
                    repo="test/model", file="new.gguf", category="loras"), 1)
                self.assertEqual(added["status"], "running")
                self.assertEqual((await self.run_op("prepare_environment", active="base",
                    request_id="second"))["status"], "failed")
                with self.assertRaises(ValueError):
                    self.c.journal.enqueue({"prompt": {"1": {}}})
                release.set()
                await asyncio.wait_for(booting.wait(), 1)
                # The Manager HTTP port is not ready while ComfyUI imports nodes.
                status = await asyncio.wait_for(self.run_op("get_split_status"), 1)
                self.assertEqual(status["candidate"], {"version": "copy", "status": "creating"})
                self.assertIsNone(status["manager_queue"])
                self.assertEqual((await self.run_op("get_admin_operation", request_id="prepare"))["status"], "running")
            finally:
                release.set()
                ready.set()
                await task
            clone.assert_called_once_with("base")
            self.c.candidate.start.assert_awaited_once_with("copy", cpu=True, manager=True)
            result = await self.run_op("get_admin_operation", request_id="prepare")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["candidate"], "copy")
            self.assertEqual(self.c.journal.data["candidate"], {"version": "copy", "status": "editing"})
            self.assertEqual(self.c.journal.data["environment"], "base")
            self.assertFalse(self.c.background_work())
            self.assertEqual(await self.run_op("prepare_environment", **args), result)

    async def test_candidate_preparation_failures_are_persisted_and_leave_active_environment(self):
        """Copy and startup failures must preserve the active version and release CPU work."""
        for failure in ("copy", "startup"):
            with self.subTest(failure=failure):
                self.c.journal.data["candidate"] = None
                self.c.candidate.start = AsyncMock(side_effect=RuntimeError("private startup detail")
                    if failure == "startup" else None)
                with patch.object(gateway, "create_environment", return_value="copy",
                        side_effect=OSError("private copy detail") if failure == "copy" else None):
                    await self.run_op("prepare_environment", active="base", request_id=failure)
                    with self.assertLogs("comfy_split.split_admin", level="ERROR"):
                        await self.c.prepare_task
                result = await self.run_op("get_admin_operation", request_id=failure)
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("private", str(result))
                self.assertEqual(self.c.journal.data["environment"], "base")
                self.assertFalse(self.c.background_work())
                self.assertIsNone(self.c.prepare_task)
                if failure == "copy":
                    self.assertIsNone(self.c.journal.data["candidate"])
                else:
                    self.assertEqual(self.c.journal.data["candidate"], {"version": "copy", "status": "failed"})
                    discarded = await self.run_op("discard_environment", candidate="copy", request_id="discard")
                    self.assertEqual(discarded["status"], "completed")

    async def test_probe_timeout_and_missing_executable_have_distinct_errors(self):
        (self.dirs["input"] / "test.wav").write_bytes(b"RIFFxxxxWAVE")
        for error, expected in ((subprocess.TimeoutExpired("ffprobe", 15), web.HTTPBadRequest),
                                (FileNotFoundError("private executable path"), web.HTTPServiceUnavailable)):
            with patch.object(media.subprocess, "run", side_effect=error), self.assertRaises(expected) as caught:
                await media.handle(self.c, "inspect_media", {"filename": "test.wav"})
            self.assertNotIn("private", caught.exception.text)

    async def test_output_indexes_match_list_outputs_when_gif_and_3d_precede(self):
        job = self.c.journal.enqueue({"prompt": {"1": {"class_type": "SaveImage", "inputs": {}}}})
        Image.new("RGB", (8, 8)).save(self.dirs["output"] / "a.gif")
        Image.new("RGB", (64, 64)).save(self.dirs["output"] / "b.png")
        (self.dirs["output"] / "mesh.GLB").write_bytes(b"glTF fixture")
        (self.dirs["output"] / "points.spz").write_bytes(b"\x1f\x8b fixture")
        job.update(status="completed", history={"outputs": {"1": {"images": [
            {"filename": "a.gif", "subfolder": "", "type": "output"}], "3d": [
            {"filename": name, "subfolder": "", "type": "output"} for name in ("mesh.GLB", "points.spz")]},
            "2": {"images": [{"filename": "b.png", "subfolder": "", "type": "output"}]}}})
        read = lambda name, **a: media.handle(self.c, name, {"prompt_id": job["id"], **a})
        rows = (await read("list_outputs"))["items"]
        self.assertEqual([r["filename"] for r in rows], ["a.gif", "mesh.GLB", "points.spz", "b.png"])
        self.assertEqual((await read("inspect_media", index=3))["width"], 64)
        with patch.object(media.subprocess, "run") as probe:
            for index in (1, 2):
                name = rows[index]["filename"]
                self.assertEqual(await read("inspect_media", index=index), {
                    "filename": name, "kind": "3d", "size_bytes": (self.dirs["output"] / name).stat().st_size,
                    "format": Path(name).suffix[1:].upper()})
                with self.assertRaises(web.HTTPBadRequest) as caught:
                    await read("reuse_output", index=index)
                self.assertEqual(caught.exception.text, "3D outputs cannot be reused as inputs")
            probe.assert_not_called()
        self.assertEqual(list(self.dirs["input"].iterdir()), [])
        self.c.volumes["input"].commit.aio.assert_not_awaited()
        # GIF is not an accepted input format; it must fail, not reuse b.png.
        with self.assertRaises(web.HTTPBadRequest):
            await read("reuse_output", index=0)

    async def test_3d_uploads_and_input_listing_remain_unsupported(self):
        for name in ("mesh.GLB", "points.spz", "existing.gif", "existing.wav", "existing.mp4"):
            (self.dirs["input"] / name).write_bytes(b"fixture")
        rows = (await media.handle(self.c, "list_inputs", {}))["items"]
        self.assertEqual([r["filename"] for r in rows], ["existing.gif", "existing.mp4", "existing.wav"])
        with self.assertRaises(web.HTTPBadRequest) as caught:
            await media.handle(self.c, "list_inputs", {"kind": "3d"})
        self.assertEqual(caught.exception.text, "Unknown media kind")
        for name in ("mesh.GLB", "points.spz"):
            transfer = media.media.Transfers(self.dirs["input"], "a" * 64)
            with self.assertRaises(web.HTTPBadRequest) as caught:
                transfer.apply("status", name)
            self.assertEqual(caught.exception.text, "Unsupported filename extension")
            self.assertFalse(transfer.part.exists())
            self.assertFalse(transfer.meta.exists())

    async def test_job_output_reuse_preserves_bytes_and_graph_and_never_requeues(self):
        job = self.c.journal.enqueue({"prompt": {"1": {"class_type": "SaveImage", "inputs": {}}}})
        out = self.dirs["output"] / "sub"
        out.mkdir()
        Image.new("RGB", (64, 64), "blue").save(out / "original.png")
        job.update(
            status="completed",
            history={
                "outputs": {
                    "1": {
                        "images": [
                            {"filename": "original.png", "subfolder": "sub", "type": "output"},
                            {"filename": "other.png", "subfolder": "..", "type": "output"},
                            {"filename": "temp.png", "subfolder": "", "type": "temp"},
                        ]
                    }
                }
            },
        )
        args = {"prompt_id": job["id"]}
        read = lambda name, **a: media.handle(self.c, name, {**args, **a})
        self.assertEqual((await read("list_outputs"))["total"], 1)
        self.assertEqual((await read("get_job_workflow"))["workflow"], job["body"]["prompt"])
        item = await read("reuse_output", index=0)
        self.assertEqual(await read("reuse_output", index=0), item)
        self.assertEqual(
            (self.dirs["input"] / item["filename"]).read_bytes(), (out / "original.png").read_bytes()
        )
        self.assertEqual((await read("inspect_media", index=0))["width"], 64)
        self.assertEqual((await media.handle(self.c, "list_inputs", {"kind": "image"}))["total"], 1)
        self.assertEqual(len(self.c.journal.data["jobs"]), 1)
        with self.assertRaises(web.HTTPBadRequest):
            await read("reuse_output", index=1)
        with self.assertRaises(web.HTTPNotFound):
            await media.handle(self.c, "list_outputs", {"prompt_id": str(uuid4())})
        (self.dirs["input"] / "link.png").symlink_to(out / "original.png")
        with self.assertRaises(web.HTTPNotFound):
            await media.handle(self.c, "inspect_media", {"filename": "link.png"})
        with self.assertRaises(web.HTTPBadRequest):
            await media.handle(self.c, "inspect_media", {"filename": "../original.png"})

    async def test_model_remove_and_restore_are_versioned_recoverable_and_single_file(self):
        folder = self.dirs["models"] / "loras"
        folder.mkdir()
        path = "loras/a.safetensors"
        f = self.dirs["models"] / path
        f.write_bytes(b"old")
        version = (await self.run_op("inspect_model", path=path))["version"]
        f.write_bytes(b"newer")
        self.assertEqual(
            (await self.run_op("remove_model", path=path, version=version, request_id="stale"))[
                "status"
            ],
            "failed",
        )
        version = (await self.run_op("inspect_model", path=path))["version"]
        result = await self.run_op("remove_model", path=path, version=version, request_id="remove")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result, await self.run_op("remove_model", path=path, version=version, request_id="remove")
        )
        self.assertFalse(f.exists())
        f.write_bytes(b"replacement")
        self.assertEqual(
            (await self.run_op("restore_model", removal_id="remove", request_id="conflict"))["status"],
            "failed",
        )
        self.assertEqual(f.read_bytes(), b"replacement")
        f.unlink()
        self.assertEqual(
            (await self.run_op("restore_model", removal_id="remove", request_id="restore"))["status"],
            "completed",
        )
        self.assertEqual(f.read_bytes(), b"newer")
        (folder / "link.pt").symlink_to(f)
        with self.assertRaises(ValueError):
            models.inspect(self.dirs["models"], "loras/link.pt")

    async def test_cpu_download_receipt_survives_timeout_and_conflicting_retries(self):
        args = dict(request_id="add", repo="test/weights", file="a.gguf", category="loras")
        with (
            patch.object(admin, "start_download", AsyncMock(return_value="private-call-id")) as spawn,
            patch.object(
                admin,
                "download_result",
                AsyncMock(side_effect=[TimeoutError(), {"path": "loras/a.gguf"}]),
            ),
        ):
            first = await self.run_op("add_model", **args)
            self.assertNotIn("private-call-id", str(first))
            self.assertEqual((await self.run_op("add_model", **args))["status"], "running")
            with self.assertRaises(web.HTTPConflict):
                await self.run_op("add_model", **{**args, "file": "b.gguf"})
            self.assertEqual(
                (await self.run_op("get_admin_operation", request_id="add"))["status"], "completed"
            )
            spawn.assert_awaited_once()
        for bad in ("../a.gguf", "https://other/b.gguf", "pickle.pt"):
            with self.assertRaises(ValueError):
                models.import_spec("test/repo", bad, "loras")
        with self.assertRaises(web.HTTPNotFound):
            await self.run_op("get_admin_operation", request_id="not-an-operation")

    async def test_candidate_receipt_waits_for_real_task_then_validates_exact_candidate(self):
        c = self.c
        c.journal.data["candidate"] = {"version": "copy", "status": "editing"}
        c.ensure_candidate = AsyncMock()
        c.candidate.process = SimpleNamespace(returncode=None)
        state = {"is_processing": True, "pending_count": 0, "in_progress_count": 1}
        history = {}

        async def manager(c, method, path, **kw):
            if path.endswith("/status"):
                return state
            if path.endswith("/history"):
                return {"history": history}
            if path.endswith("/task"):
                self.assertEqual(kw["json"]["params"]["id"], "example")
            return None

        with patch.object(admin, "manager", side_effect=manager) as mgr:
            args = dict(request_id="node", candidate="copy", node="example", action="install")
            first = await self.run_op("change_node", **args)
            self.assertEqual(first["status"], "running")
            self.assertEqual((await self.run_op("change_node", **args))["status"], "running")
            self.assertEqual(len([c for c in mgr.call_args_list if c.args[2].endswith("/task")]), 1)
            self.assertEqual(
                (await self.run_op("apply_environment", candidate="copy", request_id="early"))["status"],
                "failed",
            )
            history.update(status={"completed": True, "status_str": "success"})
            state.update(is_processing=False, in_progress_count=0)
            await admin.refresh_nodes(c, state)
            self.assertEqual(
                (await self.run_op("get_admin_operation", request_id="node"))["status"], "completed"
            )
            self.assertEqual(
                (await self.run_op("discard_environment", candidate="other", request_id="wrong"))[
                    "status"
                ],
                "failed",
            )
            c.apply_environment = AsyncMock()
            self.assertEqual(
                (await self.run_op("apply_environment", candidate="copy", request_id="apply"))["status"],
                "running",
            )
            await c.apply_task
            c.apply_environment.assert_awaited_once()
            admin.settle_apply(c, "copy", False)
            self.assertEqual(
                (await self.run_op("get_admin_operation", request_id="apply"))["status"], "failed"
            )
            self.assertEqual(c.journal.data["environment"], "base")

    def test_import_pins_revision_and_hashes_before_publishing(self):
        import hashlib
        import sys

        commit = "a" * 40
        payload = b"model fixture"

        def fetch(repo, file, revision, local_dir):
            self.assertEqual(revision, commit)
            src = Path(local_dir) / file
            src.write_bytes(payload)
            return str(src)

        hub = SimpleNamespace(
            HfApi=lambda: SimpleNamespace(model_info=lambda repo, revision: SimpleNamespace(sha=commit)),
            hf_hub_download=fetch,
        )
        root = self.dirs["models"]
        args = ("test/repo", "sample.safetensors", "checkpoints")
        with patch.dict(sys.modules, huggingface_hub=hub):
            with self.assertRaisesRegex(ValueError, "checksum"):
                models.download(root, *args, sha256="0" * 64)
            self.assertFalse((root / "checkpoints/sample.safetensors").exists())
            digest = hashlib.sha256(payload).hexdigest()
            result = models.download(root, *args, sha256=digest)
            self.assertEqual(result["revision"], commit)
            self.assertEqual(result["sha256"], digest)
            with self.assertRaisesRegex(ValueError, "already in use"):
                models.download(root, *args)
        self.assertEqual((root / result["path"]).read_bytes(), payload)

    async def test_hash_id_survives_new_inode_and_large_inspections_report_progress(self):
        f = self.dirs["models"] / "loras" / "a.gguf"
        f.parent.mkdir()
        f.write_bytes(b"same")
        first = (await self.run_op("inspect_model", path="loras/a.gguf"))["version"]
        # A new mount can give an identical blob a different inode.
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(b"same")
        tmp.replace(f)
        with patch.object(admin, "INLINE_HASH_LIMIT", 1):
            row = await self.run_op("inspect_model", path="loras/a.gguf")
            self.assertEqual(row["status"], "running")
            self.assertTrue(self.c.background_work())
            await self.c.model_hash_task
            info = await self.run_op("get_admin_operation", request_id=row["request_id"])
            self.assertEqual(info["result"]["version"], first)
            f.write_bytes(b"diff")
            row = await self.run_op(
                "remove_model", request_id="stale-hash", path="loras/a.gguf", version=first
            )
            await self.c.model_hash_task
            self.assertEqual(
                (await self.run_op("get_admin_operation", request_id="stale-hash"))["status"], "failed"
            )
            self.assertTrue(f.exists())
            self.assertFalse(self.c.background_work())
