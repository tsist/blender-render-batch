"""Encode ordered, verified sRGB frames and certify the review movie."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

COLOR_FILTER = (
    "format=rgb24,zscale=matrixin=gbr:primariesin=bt709:"
    "transferin=iec61966-2-1:rangein=full:matrix=bt709:"
    "primaries=bt709:transfer=bt709:range=limited:dither=error_diffusion,format=yuv420p"
)


def run_tool(executable, options, directory, seconds):
    done = subprocess.run([str(executable), *options], cwd=directory, check=False,
                          capture_output=True, timeout=seconds,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        tail = done.stderr.decode("utf-8", "replace")[-1400:].strip()
        raise RuntimeError(f"{Path(executable).name} failed ({done.returncode}): {tail}")
    return done.stdout


def preflight_tools(ffmpeg, ffprobe, directory):
    filters = run_tool(ffmpeg, ["-hide_banner", "-filters"], directory, 20).decode("utf-8", "replace")
    encoders = run_tool(ffmpeg, ["-hide_banner", "-encoders"], directory, 20).decode("utf-8", "replace")
    run_tool(ffprobe, ["-version"], directory, 20)
    if "zscale" not in filters.split() or "libx264" not in encoders.split():
        raise RuntimeError("FFmpeg requires zscale and libx264 for BT.709 review output")


def verify_video(ffmpeg, ffprobe, name, directory, count, width, height, fps):
    raw = run_tool(ffprobe, [
        "-v", "error", "-show_streams", "-show_frames", "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,color_range,color_transfer,color_space,color_primaries,avg_frame_rate,time_base:frame=best_effort_timestamp",
        "-of", "json", name,
    ], directory, max(60, count))
    info = json.loads(raw)
    streams = info.get("streams", [])
    expected = {
        "codec_type": "video", "codec_name": "h264", "width": width, "height": height,
        "pix_fmt": "yuv420p", "avg_frame_rate": f"{fps}/1",
        "color_range": "tv", "color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709",
    }
    if len(streams) != 1 or any(streams[0].get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Review stream differs from the delivery profile: {streams}")
    time_base = Fraction(streams[0]["time_base"])
    stamps = [int(frame["best_effort_timestamp"]) * time_base for frame in info.get("frames", [])]
    if stamps != [Fraction(index, fps) for index in range(count)]:
        raise RuntimeError("Review frame count or playback timestamps differ from the shot")
    run_tool(ffmpeg, ["-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                      "-i", name, "-map", "0:v:0", "-f", "null", "-"], directory, max(60, count))
    return {"primaries": "bt709", "transfer": "bt709", "matrix": "bt709", "range": "tv",
            "conversion": "full sRGB to limited BT.709 using zscale"}


def encode_sequence(frames, video, *, ffmpeg, ffprobe, width, height, fps=24):
    """Publish one reviewed movie after checking every decoded frame and timestamp."""
    output = Path(video).resolve()
    paths = [Path(frame).resolve() for frame in frames]
    if not paths or fps != 24 or any(value <= 0 or value % 2 for value in (width, height)):
        raise ValueError("Review requires even dimensions and ordered frames at 24 fps")
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Review destination must be a new MP4 path")
    if any(path.suffix.lower() != ".png" or not path.is_file() for path in paths):
        raise ValueError("Review frames must be existing verified PNGs")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="review-") as workspace:
        stage = Path(workspace)
        for index, path in enumerate(paths, start=1):
            frame = stage / f"frame-{index:06d}.png"
            try:
                os.link(path, frame)
            except OSError:
                shutil.copyfile(path, frame)
        name = "candidate.mp4"
        run_tool(ffmpeg, [
            "-nostdin", "-v", "error", "-xerror", "-n", "-framerate", str(fps), "-start_number", "1",
            "-i", "frame-%06d.png", "-map", "0:v:0", "-frames:v", str(len(paths)),
            "-vf", COLOR_FILTER, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-movflags", "+faststart+write_colr", "-an", name,
        ], stage, max(120, len(paths)))
        color = verify_video(ffmpeg, ffprobe, name, stage, len(paths), width, height, fps)
        candidate = stage / name
        with candidate.open("rb+") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            os.fsync(handle.fileno())
        # A hard link publishes the already verified bytes atomically and cannot replace an existing file.
        os.link(candidate, output)
    return {"path": str(output), "width": width, "height": height, "fps": fps, "frames": len(paths),
            "sha256": digest, "color": color, "codec": "h264"}


def placeholder(card, shot_id, *, ffmpeg):
    """Produce a frame for the exact timeline slot of an unfinished shot."""
    if not shot_id.isascii() or not shot_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid shot id for placeholder")
    font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    if not font.is_file():
        raise RuntimeError("The local review font is unavailable")
    with tempfile.TemporaryDirectory(dir=card.parent, prefix="caption-") as directory:
        stage = Path(directory)
        shutil.copyfile(font, stage / "label.ttf")
        draw = ("drawtext=fontfile=label.ttf:fontcolor=white:fontsize=74:x=(w-tw)/2:y=(h-th)/2"
                f":text='INCOMPLETE  /  {shot_id}'")
        run_tool(ffmpeg, ["-nostdin", "-v", "error", "-xerror", "-n", "-f", "lavfi",
                          "-i", "color=c=0x202128:s=1920x1080:r=24", "-vf", draw,
                          "-frames:v", "1", "-threads", "1", "-update", "1", "card.png"],
                 stage, 60)
        os.link(stage / "card.png", card)

