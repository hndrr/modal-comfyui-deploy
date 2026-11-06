import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from PIL import Image

from comfy_split.input_media import MAX, Transfers

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.t = Transfers(self.root, "a"*64)
        stream = io.BytesIO()
        Image.new("RGB", (8, 8)).save(stream, "PNG")
        self.png = stream.getvalue()

    def test_resume_replay_and_publish(self):
        self.t.apply("status", "photo.png")
        self.t.apply("part", data=self.png[:16])
        other = Transfers(self.root, self.t.id)
        self.assertEqual(other.apply("status", "photo.png")["next_offset"], 16)
        other.apply("part", data=self.png[:16])
        for offset, data in [(0,b"x"), (17,b"x"), (15,self.png[15:17])]:
            with self.assertRaises(web.HTTPConflict):
                other.apply("part", offset=offset, data=data)
        self.assertFalse(list(self.root.glob("*.png")))
        other.apply("part", offset=16, data=self.png[16:])
        with self.assertRaises(web.HTTPBadRequest):
            other.apply("finish", total=len(self.png)+1)
        receipt = other.apply("finish", total=len(self.png))
        self.assertEqual(other.apply("finish", total=len(self.png)), receipt)
        self.assertEqual((self.root/receipt["filename"]).read_bytes(), self.png)
        with self.assertRaises(web.HTTPConflict):
            other.apply("status","different.png")

    def test_interrupted_publication_is_recovered_and_name_collision_is_not_overwritten(self):
        self.t.apply("status", "probe.png")
        self.t.apply("part", data=self.png)
        dest = self.root / ("mcp-"+hashlib.sha256(self.png).hexdigest()+".png")
        dest.write_bytes(b"existing file")
        with self.assertRaises(web.HTTPConflict):
            self.t.apply("finish", total=len(self.png))
        self.assertEqual(dest.read_bytes(), b"existing file")
        dest.unlink()
        with patch("comfy_split.input_media.os.replace", side_effect=OSError("lost before rename")):
            with self.assertRaises(OSError):
                self.t.apply("status", "probe.png")
        self.assertEqual(dest.read_bytes(), self.png)
        receipt = Transfers(self.root, self.t.id).apply("status", "probe.png")
        self.assertTrue(receipt["complete"])
        self.assertFalse(dest.is_symlink())
        with self.assertRaises(web.HTTPConflict):
            self.t.apply("part", data=b"x")

    def test_bad_files_or_oversize_never_publish(self):
        self.t.apply("status","video.mp4")
        with self.assertRaises(web.HTTPBadRequest):
            self.t.apply("part",offset=MAX,data=b"x")
        self.t.apply("part",data=self.png)
        with self.assertRaises(web.HTTPBadRequest):
            self.t.apply("finish",total=len(self.png))
        self.assertFalse(list(self.root.glob("*.mp4")))
        with self.assertRaises(web.HTTPBadRequest):
            Transfers(self.root,"../bad")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg")
    def test_audio_and_video_containers(self):
        for name,source in [("sample.wav","anullsrc=r=8000:cl=mono"),("sample.mp4","color=size=32x32")]:
            with self.subTest(name=name):
                path = self.root/name
                subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i",source,"-t","0.2",str(path)],check=True)
                data = path.read_bytes()
                t = Transfers(self.root,hashlib.sha256(data).hexdigest())
                t.apply("status",name)
                t.apply("part",data=data)
                receipt=t.apply("finish",total=len(data))
                self.assertEqual((self.root/receipt["filename"]).read_bytes(),data)
