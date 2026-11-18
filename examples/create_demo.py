"""Build two self-contained demo shots and a local batch request in Blender."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

import bpy
from mathutils import Vector


def aim(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat("-Z", "Y").to_euler()


def area(scene, name, location, energy, size):
    light = bpy.data.lights.new(name, "AREA")
    light.energy = energy
    light.shape = "DISK"
    light.size = size
    obj = bpy.data.objects.new(name, light)
    scene.collection.objects.link(obj)
    obj.location = location
    aim(obj, (0, 0, 0))


def material(name, color, roughness):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Roughness"].default_value = roughness
    return mat


def make_shot(target, ident, engine, color, motion_blur):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = ident
    scene.render.engine = engine
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080
    scene.render.resolution_percentage = 100
    scene.render.fps = 24
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.use_motion_blur = motion_blur
    scene.frame_end = 6
    scene.display_settings.display_device = "sRGB"
    scene.view_settings.view_transform = "Standard"
    if engine == "CYCLES":
        scene.cycles.samples = 4
    else:
        scene.eevee.taa_render_samples = 8

    bpy.ops.mesh.primitive_plane_add(size=200)
    bpy.context.object.data.materials.append(material("Matte floor", (0.18, 0.16, 0.14), 0.8))
    if motion_blur:
        bpy.ops.mesh.primitive_cube_add(size=1.6, location=(0, 0, 0.8))
        bevel = bpy.context.object.modifiers.new("Soft edges", "BEVEL")
        bevel.width = 0.14
        bevel.segments = 4
    else:
        bpy.ops.mesh.primitive_torus_add(major_radius=0.8, minor_radius=0.24,
                                         location=(0, 0, 0.64), rotation=(0.5, 0.2, 0))
    shape = bpy.context.object
    shape.data.materials.append(material("Hero", color, 0.25))
    shape.location.x = -0.55
    shape.keyframe_insert("location", frame=1)
    shape.location.x = 0.55
    shape.keyframe_insert("location", frame=6)

    camera = bpy.data.objects.new("ReviewCamera", bpy.data.cameras.new("ReviewCamera"))
    scene.collection.objects.link(camera)
    camera.location = (4.4, -7, 3.2)
    aim(camera, (0, 0, 0.55))
    camera.data.lens = 50
    scene.camera = camera
    area(scene, "Key", (0, -4, 5), 550, 4)
    area(scene, "Fill", (3, 3, 4), 350, 3)
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=str(target))

    with target.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"id": ident, "project": str(target), "expected_sha256": digest, "scene": scene.name,
            "view_layer": scene.view_layers[0].name, "camera": camera.name, "frame_start": 1, "frame_end": 6,
            "settings": "source", "device": {"backend": "CPU" if engine == "CYCLES" else "GRAPHICS"}}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--backend", choices=("local", "colab"), default="local")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    cloud = args.backend == "colab"
    inputs = out / "inputs" if cloud else out
    inputs.mkdir(exist_ok=True)
    shots = [
        make_shot(inputs / "box.blend", "red-box", "CYCLES" if cloud else "BLENDER_EEVEE",
                  (0.65, 0.04, 0.025), True),
        make_shot(inputs / "torus.blend", "blue-torus", "CYCLES", (0.035, 0.18, 0.52), False),
    ]
    if cloud:
        request = {
            "version": "1.0",
            "shot_order": [shot["id"] for shot in shots],
            "shots": [
                {"shot_id": shot["id"], **{key: shot[key] for key in
                 ("scene", "view_layer", "camera", "frame_start", "frame_end")},
                 "blend": {"file": Path(shot["project"]).name, "sha256": shot["expected_sha256"],
                           "packaging": "single_self_contained_blend"},
                 "fps": 24, "settings": {"engine": "CYCLES", "color_management": {"display_device": "sRGB"}},
                 "resolution": {"width": 1920, "height": 1080, "percentage": 100}}
                for shot in shots
            ],
        }
    else:
        request = {"version": "1.0", "backend": "local", "shots": shots, "cpu_threads": 4,
                   "delivery": {"width": 1920, "height": 1080, "fps": [24, 1], "color": "SDR",
                                "review_codec": "h264"}}
    (out / "render-request.json").write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    print(out / "render-request.json")


if __name__ == "__main__":
    main()
