"""Durable library metadata; media is outside the temporary job namespace.

Deletion uses a tombstone so a late tagging worker cannot resurrect a clip.
Media is published before its metadata becomes visible.
"""
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .contracts import identifier
from .media import finalize


class Library:
    def __init__(self, records, volume, now=time.time):
        self.records, self.volume, self.now = records, volume, now

    def list(self):
        clips = []
        for key, value in self.records.items():
            if key.startswith("clip:") and not self.records.get("deleted:" + value["id"]):
                try:
                    clips.append(self.get(value["id"]))
                except KeyError:
                    pass  # A concurrent deletion is already absent from this listing.
        clips.sort(key=lambda c: (c["createdAt"], c["id"]), reverse=True)
        return {"clips": clips, "count": len(clips), "bytes": sum(c.get("bytes", 0) for c in clips)}

    def get(self, clip_id):
        identifier(clip_id)
        clip = self.records.get("clip:" + clip_id)
        if not clip or self.records.get("deleted:" + clip_id):
            raise KeyError(clip_id)
        return {**clip, "tagging": self.records.get("tags:" + clip_id, {"status": "untagged"})}

    def publish(self, source, clip, *, name="", generation=None):
        clip_id = identifier(clip["id"])
        if self.records.get("deleted:" + clip_id):
            raise ValueError("Clip was deleted")
        if self.records.get("clip:" + clip_id):
            return self.get(clip_id)
        with self.volume.batch_upload() as batch:
            batch.put_file(str(source), f"ambient/library/{clip_id}.mp4")
        if self.records.get("deleted:" + clip_id):
            self.volume.remove_file(f"ambient/library/{clip_id}.mp4")
            raise KeyError(clip_id)
        value = {**clip, "name": name[:200] or clip_id, "createdAt": self.now(),
                 "generation": generation}
        self.records.put("clip:" + clip_id, value, skip_if_exists=True)
        return self.get(clip_id)

    def import_video(self, source, name):
        clip_id = str(uuid4())
        with tempfile.TemporaryDirectory(prefix="ambient-import-") as directory:
            root = Path(directory)
            clip = finalize(Path(source), root / "clip.mp4", root / "last.png", clip_id,
                            require_audio=False)
            return self.publish(root / "clip.mp4", clip, name=name)

    def delete(self, clip_id):
        self.get(clip_id)
        self.records.put("deleted:" + clip_id, True)
        self.volume.remove_file(f"ambient/library/{clip_id}.mp4")

    def download(self, clip_id, target):
        self.get(clip_id)
        with Path(target).open("wb") as handle:
            self.volume.read_file_into_fileobj(f"ambient/library/{clip_id}.mp4", handle)

    def tags(self, clip_id, result):
        self.get(clip_id)
        self.records.put("tags:" + clip_id, result)
