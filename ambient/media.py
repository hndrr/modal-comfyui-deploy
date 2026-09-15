from __future__ import annotations

import json
import subprocess
from pathlib import Path


def run(args: list[str]) -> None:
    process = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if process.returncode:
        raise RuntimeError(f"Media processing failed: {process.stderr[-1600:]}")


def finalize(source: Path, destination: Path, final_frame: Path, clip_id: str) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    final_frame.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part.mp4")
    try:
        # The explicit audio map rejects silent/missing-audio results.
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "19",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
        )
        info = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(temporary),
                ],
                timeout=60,
            )
        )
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        if not audio:
            raise RuntimeError("The generated clip has no audio track")
        frames = int(video["nb_read_frames"])
        if frames < 2:
            raise RuntimeError("The generated clip contains fewer than two frames")
        # Decode and select the exact last frame; seeking duration-epsilon can return the wrong frame.
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(temporary),
                "-vf",
                f"select=eq(n\\,{frames - 1})",
                "-frames:v",
                "1",
                str(final_frame),
            ]
        )
        if not final_frame.exists():
            raise RuntimeError("Final frame extraction failed")
        duration = float(video.get("duration") or info["format"]["duration"])
        if abs(float(audio.get("duration", duration)) - duration) > 0.25:
            raise RuntimeError("Generated video and audio durations do not match")
        numerator, denominator = video["avg_frame_rate"].split("/")
        fps = float(numerator) / float(denominator)
        destination_tmp_size = temporary.stat().st_size
        temporary.replace(destination)
        return {
            "id": clip_id,
            "duration": duration,
            "fps": fps,
            "width": video["width"],
            "height": video["height"],
            "frames": frames,
            "hasAudio": True,
            "bytes": destination_tmp_size,
        }
    finally:
        temporary.unlink(missing_ok=True)
