#!/usr/bin/env python3
"""Render requested frames with Cycles from an already opened Blender file."""
import json
import os
import sys

RESULT_PREFIX = "COLAB_RENDER_RESULT"
FRAME_PREFIX = "COLAB_FRAME_RESULT"


class WorkerError(Exception):
    pass


def check(condition, message):
    if not condition:
        raise WorkerError(message)


def is_int(value):
    return type(value) is int


def load_work(argv):
    try:
        separator = argv.index("--")
    except ValueError as exc:
        raise WorkerError("missing '-- <work.json>' arguments") from exc
    args = argv[separator + 1:]
    check(len(args) == 1, "expected exactly one work JSON path after '--'")
    path = args[0]
    check(os.path.isfile(path), f"work JSON is not a file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read work JSON {path}: {exc}") from exc


def validate(work):
    check(isinstance(work, dict), "work JSON must be an object")
    for key in ("scene", "camera", "view_layer"):
        check(isinstance(work.get(key), str) and work[key], f"{key} must be a nonempty string")
    frames = work.get("frames")
    check(isinstance(frames, list) and frames, "frames must be a nonempty list")
    check(all(is_int(frame) for frame in frames), "frames must contain integers only")
    check(len(set(frames)) == len(frames), "frames must be unique")
    output_dir = work.get("output_dir")
    check(isinstance(output_dir, str) and os.path.isabs(output_dir) and os.path.isdir(output_dir),
          "output_dir must be an existing absolute directory")
    percent = work.get("resolution_percent")
    check(is_int(percent) and 1 <= percent <= 100, "resolution_percent must be an integer from 1 to 100")
    check(work.get("device") in ("CPU", "CUDA"), "device must be CPU or CUDA")
    check(isinstance(work.get("expected_gpu"), str), "expected_gpu must be a string")
    check(is_int(work.get("fps")), "fps must be an integer")
    dimensions = work.get("dimensions")
    check(isinstance(dimensions, list) and len(dimensions) == 2
          and all(is_int(value) and value > 0 for value in dimensions),
          "dimensions must be [width,height] positive integers")
    threads = work.get("threads")
    check(is_int(threads) and threads >= 0, "threads must be 0 or a positive integer")
    check(work.get("expected_engine") == "CYCLES", "expected_engine must be CYCLES")


def select(bpy, work):
    scene = bpy.data.scenes.get(work["scene"])
    check(scene is not None, f"scene not found: {work['scene']}")
    camera = scene.objects.get(work["camera"])
    check(camera is not None and camera.type == "CAMERA",
          f"camera object not found in scene or not a camera: {work['camera']}")
    # Marker bindings are scene-local and can change scene.camera during evaluation.
    # Blender is operating on a frozen, read-only working copy.
    for marker in scene.timeline_markers:
        if marker.camera is not None and marker.camera != camera:
            marker.camera = None
    scene.camera = camera
    layer = scene.view_layers.get(work["view_layer"])
    check(layer is not None, f"view layer not found: {work['view_layer']}")
    return scene, camera, layer


def configure_render(scene, work):
    render = scene.render
    check(render.engine == "CYCLES", f"source render engine is {render.engine}, expected CYCLES")
    dimensions = [render.resolution_x, render.resolution_y]
    check(dimensions == work["dimensions"],
          f"source base dimensions {dimensions} != requested {work['dimensions']}")
    check(render.fps_base > 0, "source fps_base must be positive")
    actual_fps = render.fps / render.fps_base
    check(actual_fps == float(work["fps"]),
          f"source FPS {actual_fps:g} != requested {work['fps']}")
    check(scene.display_settings.display_device == "sRGB", "review frames must use the sRGB display device")
    image = render.image_settings
    check(image.file_format == "PNG", f"source image format is {image.file_format}, expected PNG")
    check(image.color_mode == "RGB", f"source PNG color mode is {image.color_mode}, expected RGB")
    check(image.color_depth == "8", f"source PNG color depth is {image.color_depth}, expected 8")
    image.file_format = "PNG"
    image.color_mode = "RGB"
    image.color_depth = "8"
    render.resolution_percentage = work["resolution_percent"]
    render.threads_mode = "AUTO" if work["threads"] == 0 else "FIXED"
    if work["threads"]:
        render.threads = work["threads"]
    render.use_file_extension = False


def configure_device(bpy, scene, work):
    if work["device"] == "CPU":
        scene.cycles.device = "CPU"
        return "CPU", "CPU", "CPU"
    addon = bpy.context.preferences.addons.get("cycles")
    check(addon is not None, "Cycles add-on preferences are unavailable")
    preferences = addon.preferences
    try:
        preferences.compute_device_type = "CUDA"
        preferences.get_devices()
    except Exception as exc:
        raise WorkerError(f"CUDA device enumeration failed: {exc}") from exc
    needle = work["expected_gpu"].lower()
    matches = [device for device in preferences.devices
               if device.type == "CUDA" and (not needle or needle in device.name.lower())]
    check(bool(matches), f"no CUDA device matches expected_gpu={work['expected_gpu']!r}")
    chosen = matches[0]
    for device in preferences.devices:
        device.use = False
    chosen.use = True
    scene.cycles.device = "GPU"
    check(chosen.use and scene.cycles.device == "GPU", "failed to activate the selected CUDA device")
    return "CUDA", "CUDA", chosen.name


def render_frames(bpy, scene, camera, layer, work, runtime):
    paths = [os.path.join(work["output_dir"], f"frame_{frame:06d}.png") for frame in work["frames"]]
    existing = [path for path in paths if os.path.lexists(path)]
    check(not existing, f"refusing to overwrite existing staged frame(s): {existing}")
    for frame, path in zip(work["frames"], paths):
        check(not os.path.lexists(path), f"refusing to overwrite existing staged frame: {path}")
        scene.frame_set(frame)
        scene.camera = camera
        check(scene.frame_current == frame and scene.camera == camera,
              f"failed to apply frame {frame} and camera {camera.name}")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True, scene=scene.name, layer=layer.name)
        check(scene.camera == camera, f"camera changed during frame {frame}")
        check(os.path.isfile(path) and os.path.getsize(path) > 0,
              f"render did not create a nonempty frame: {path}")
        # This precedes the next frame, so the supervisor can retain output
        # from a successful frame even if the worker then exits abnormally.
        print(FRAME_PREFIX + " " + json.dumps({**runtime, "frame": frame}, ensure_ascii=True), flush=True)


def main():
    work = load_work(sys.argv)
    validate(work)
    import bpy
    scene, camera, layer = select(bpy, work)
    configure_render(scene, work)
    device, backend, device_name = configure_device(bpy, scene, work)
    build = getattr(bpy.app, "build_hash", "")
    if isinstance(build, bytes):
        build = build.decode("utf-8", "replace")
    result = {
        "status": "ok",
        "engine": scene.render.engine,
        "device": device,
        "backend": backend,
        "device_name": device_name,
        "scene": scene.name,
        "camera": camera.name,
        "view_layer": layer.name,
        "resolution_percent": scene.render.resolution_percentage,
        "frames": work["frames"],
        "output_dir": work["output_dir"],
        "blender_version": bpy.app.version_string,
        "blender_build": str(build),
    }
    render_frames(bpy, scene, camera, layer, work, result)
    print(f"{RESULT_PREFIX} {json.dumps(result, separators=(',', ':'), ensure_ascii=True)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except WorkerError as exc:
        print(f"WORKER_ERROR: {exc}", file=sys.stderr, flush=True)
        raise
