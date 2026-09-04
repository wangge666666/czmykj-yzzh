from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


FPS = 24
FRAME_END = 258
SHOT_CUT = 133


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--test-dir", type=Path)
    parser.add_argument("--render-tests", action="store_true")
    parser.add_argument("--full-resolution", action="store_true")
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.cameras,
        bpy.data.lights,
    ):
        for block in list(datablocks):
            if block.users == 0:
                datablocks.remove(block)


def make_material(
    name: str,
    color: tuple[float, float, float, float],
    roughness: float,
    metallic: float = 0.0,
) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Specular IOR Level"].default_value = 0.35
    return mat


def apply_material(obj: bpy.types.Object, mat: bpy.types.Material) -> None:
    obj.data.materials.append(mat)


def smooth(obj: bpy.types.Object) -> None:
    if obj.type == "MESH":
        for poly in obj.data.polygons:
            poly.use_smooth = True


def add_uv_ellipsoid(
    name: str,
    location: tuple[float, float, float],
    scale: tuple[float, float, float],
    mat: bpy.types.Material,
    segments: int = 40,
    rings: int = 24,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=segments,
        ring_count=rings,
        location=location,
    )
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    apply_material(obj, mat)
    smooth(obj)
    return obj


def add_beveled_cube(
    name: str,
    location: tuple[float, float, float],
    scale: tuple[float, float, float],
    mat: bpy.types.Material,
    bevel: float,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    apply_material(obj, mat)
    modifier = obj.modifiers.new("Soft bevel", "BEVEL")
    modifier.width = bevel
    modifier.segments = 4
    return obj


def add_tapered_torso(
    name: str,
    location: tuple[float, float, float],
    top_width: float,
    bottom_width: float,
    depth: float,
    height: float,
    mat: bpy.types.Material,
) -> bpy.types.Object:
    tw = top_width * 0.5
    bw = bottom_width * 0.5
    d = depth * 0.5
    h = height * 0.5
    vertices = [
        (-bw, -d, -h), (bw, -d, -h), (bw, d, -h), (-bw, d, -h),
        (-tw, -d, h), (tw, -d, h), (tw, d, h), (-tw, d, h),
    ]
    faces = [
        (0, 1, 2, 3), (4, 7, 6, 5),
        (0, 4, 5, 1), (1, 5, 6, 2),
        (2, 6, 7, 3), (4, 0, 3, 7),
    ]
    mesh = bpy.data.meshes.new(name + " Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    apply_material(obj, mat)
    bevel = obj.modifiers.new("Sculpted bevel", "BEVEL")
    bevel.width = min(top_width, bottom_width, height) * 0.16
    bevel.segments = 5
    smooth(obj)
    return obj


def add_empty(name: str, location: tuple[float, float, float]) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    obj.empty_display_type = "SPHERE"
    obj.empty_display_size = 0.08
    obj.location = location
    return obj


def copy_location(obj: bpy.types.Object, target: bpy.types.Object) -> None:
    constraint = obj.constraints.new("COPY_LOCATION")
    constraint.target = target


def add_dynamic_segment(
    name: str,
    start: bpy.types.Object,
    end: bpy.types.Object,
    radius: float,
    mat: bpy.types.Material,
) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=radius, depth=1.0)
    obj = bpy.context.object
    obj.name = name
    for vertex in obj.data.vertices:
        vertex.co.z += 0.5
    obj.data.transform(Matrix.Rotation(math.radians(-90), 4, "X"))
    apply_material(obj, mat)
    smooth(obj)
    bevel = obj.modifiers.new("Rounded edges", "BEVEL")
    bevel.width = radius * 0.28
    bevel.segments = 3
    copy_location(obj, start)
    stretch = obj.constraints.new("STRETCH_TO")
    stretch.target = end
    stretch.rest_length = 1.0
    stretch.volume = "NO_VOLUME"
    return obj


def add_joint(
    name: str,
    point: bpy.types.Object,
    radius: float,
    mat: bpy.types.Material,
) -> bpy.types.Object:
    obj = add_uv_ellipsoid(name, (0, 0, 0), (radius, radius, radius), mat, 32, 18)
    copy_location(obj, point)
    return obj


def parent_local(
    obj: bpy.types.Object,
    parent: bpy.types.Object,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float] = (0, 0, 0),
) -> None:
    obj.parent = parent
    obj.location = location
    obj.rotation_euler = rotation


def key_location(
    obj: bpy.types.Object,
    frame: int,
    location: tuple[float, float, float],
) -> None:
    obj.location = location
    obj.keyframe_insert(data_path="location", frame=frame)


def animate_point(
    point: bpy.types.Object,
    keys: list[tuple[int, tuple[float, float, float]]],
) -> None:
    for frame, location in keys:
        key_location(point, frame, location)
    if point.animation_data and point.animation_data.action:
        for curve in point.animation_data.action.fcurves:
            for key in curve.keyframe_points:
                key.interpolation = "BEZIER"


def set_render_visibility(
    objects: list[bpy.types.Object],
    first: int,
    last: int,
) -> None:
    for obj in objects:
        if obj.type not in {"MESH", "CURVE"}:
            continue
        if first > 1:
            obj.hide_render = True
            obj.keyframe_insert(data_path="hide_render", frame=1)
            obj.keyframe_insert(data_path="hide_render", frame=first - 1)
        obj.hide_render = False
        obj.keyframe_insert(data_path="hide_render", frame=first)
        obj.keyframe_insert(data_path="hide_render", frame=last)
        if last < FRAME_END:
            obj.hide_render = True
            obj.keyframe_insert(data_path="hide_render", frame=last + 1)


def make_face_details(
    prefix: str,
    head_point: bpy.types.Object,
    scale: float,
    white: bpy.types.Material,
    seam: bpy.types.Material,
) -> list[bpy.types.Object]:
    objects: list[bpy.types.Object] = []
    nose = add_uv_ellipsoid(prefix + " Nose", (0, 0, 0), (0.07 * scale, 0.13 * scale, 0.15 * scale), white, 28, 16)
    parent_local(nose, head_point, (0, -0.405 * scale, -0.02 * scale))
    objects.append(nose)
    for side in (-1, 1):
        eye = add_uv_ellipsoid(prefix + f" Eye {side}", (0, 0, 0), (0.13 * scale, 0.018 * scale, 0.035 * scale), seam, 24, 12)
        parent_local(eye, head_point, (side * 0.16 * scale, -0.425 * scale, 0.11 * scale))
        objects.append(eye)
    mouth = add_uv_ellipsoid(prefix + " Mouth", (0, 0, 0), (0.14 * scale, 0.014 * scale, 0.024 * scale), seam, 24, 10)
    parent_local(mouth, head_point, (0, -0.418 * scale, -0.19 * scale))
    objects.append(mouth)
    jaw = add_uv_ellipsoid(prefix + " Jaw", (0, 0, 0), (0.31 * scale, 0.32 * scale, 0.26 * scale), white, 32, 18)
    parent_local(jaw, head_point, (0, -0.01 * scale, -0.34 * scale))
    objects.append(jaw)
    return objects


def make_mannequin(
    prefix: str,
    origin: tuple[float, float, float],
    scale: float,
    white: bpy.types.Material,
    seam: bpy.types.Material,
) -> tuple[dict[str, bpy.types.Object], list[bpy.types.Object]]:
    ox, oy, oz = origin
    points_data = {
        "chest": (ox, oy, oz + 2.75 * scale),
        "head": (ox, oy, oz + 4.05 * scale),
        "shoulder_l": (ox - 0.78 * scale, oy, oz + 3.15 * scale),
        "shoulder_r": (ox + 0.78 * scale, oy, oz + 3.15 * scale),
        "elbow_l": (ox - 0.86 * scale, oy, oz + 2.25 * scale),
        "elbow_r": (ox + 0.86 * scale, oy, oz + 2.25 * scale),
        "wrist_l": (ox - 0.80 * scale, oy, oz + 1.45 * scale),
        "wrist_r": (ox + 0.80 * scale, oy, oz + 1.45 * scale),
        "hip_l": (ox - 0.36 * scale, oy, oz + 1.55 * scale),
        "hip_r": (ox + 0.36 * scale, oy, oz + 1.55 * scale),
        "knee_l": (ox - 0.38 * scale, oy, oz + 0.82 * scale),
        "knee_r": (ox + 0.38 * scale, oy, oz + 0.82 * scale),
        "ankle_l": (ox - 0.40 * scale, oy, oz + 0.16 * scale),
        "ankle_r": (ox + 0.40 * scale, oy, oz + 0.16 * scale),
    }
    points = {name: add_empty(prefix + " " + name, pos) for name, pos in points_data.items()}
    objects: list[bpy.types.Object] = []

    torso = add_tapered_torso(
        prefix + " Upper Torso",
        points_data["chest"],
        1.55 * scale,
        0.92 * scale,
        0.68 * scale,
        1.18 * scale,
        white,
    )
    objects.append(torso)
    abdomen = add_tapered_torso(
        prefix + " Abdomen",
        (ox, oy, oz + 2.03 * scale),
        0.92 * scale,
        0.72 * scale,
        0.52 * scale,
        0.92 * scale,
        white,
    )
    objects.append(abdomen)
    for side in (-1, 1):
        pectoral = add_uv_ellipsoid(
            prefix + f" Pectoral {side}",
            (ox + side * 0.33 * scale, oy - 0.35 * scale, oz + 2.90 * scale),
            (0.32 * scale, 0.055 * scale, 0.15 * scale),
            white,
            28,
            14,
        )
        objects.append(pectoral)
        for row, z in enumerate((2.36, 2.08, 1.82)):
            ab = add_uv_ellipsoid(
                prefix + f" Ab {side} {row}",
                (ox + side * 0.17 * scale, oy - 0.29 * scale, oz + z * scale),
                (0.17 * scale, 0.07 * scale, 0.14 * scale),
                white,
                24,
                12,
            )
            objects.append(ab)
    pelvis = add_uv_ellipsoid(prefix + " Pelvis", (ox, oy, oz + 1.50 * scale), (0.54 * scale, 0.33 * scale, 0.34 * scale), white)
    objects.append(pelvis)
    neck = add_dynamic_segment(prefix + " Neck", points["chest"], points["head"], 0.14 * scale, white)
    objects.append(neck)
    head = add_uv_ellipsoid(prefix + " Head", (0, 0, 0), (0.45 * scale, 0.41 * scale, 0.61 * scale), white)
    copy_location(head, points["head"])
    objects.append(head)
    objects.extend(make_face_details(prefix, points["head"], scale, white, seam))

    limb_specs = [
        ("Upper arm L", "shoulder_l", "elbow_l", 0.22),
        ("Upper arm R", "shoulder_r", "elbow_r", 0.22),
        ("Forearm L", "elbow_l", "wrist_l", 0.18),
        ("Forearm R", "elbow_r", "wrist_r", 0.18),
        ("Thigh L", "hip_l", "knee_l", 0.27),
        ("Thigh R", "hip_r", "knee_r", 0.27),
        ("Shin L", "knee_l", "ankle_l", 0.21),
        ("Shin R", "knee_r", "ankle_r", 0.21),
    ]
    for label, start, end, radius in limb_specs:
        objects.append(add_dynamic_segment(prefix + " " + label, points[start], points[end], radius * scale, white))

    for name in ("shoulder_l", "shoulder_r", "elbow_l", "elbow_r", "hip_l", "hip_r", "knee_l", "knee_r"):
        radius = 0.27 if "shoulder" in name or "hip" in name else 0.22
        objects.append(add_joint(prefix + " Joint " + name, points[name], radius * scale, seam))
    for name in ("wrist_l", "wrist_r"):
        hand = add_uv_ellipsoid(prefix + " Hand " + name, (0, 0, 0), (0.16 * scale, 0.09 * scale, 0.22 * scale), white, 32, 18)
        copy_location(hand, points[name])
        objects.append(hand)
        for finger_index, finger_x in enumerate((-0.105, -0.035, 0.035, 0.105)):
            finger = add_uv_ellipsoid(
                prefix + f" Finger {name} {finger_index}",
                (0, 0, 0),
                (0.030 * scale, 0.035 * scale, (0.16 + 0.015 * (2 - abs(1.5 - finger_index))) * scale),
                white,
                20,
                10,
            )
            parent_local(finger, points[name], (finger_x * scale, -0.03 * scale, 0.20 * scale))
            objects.append(finger)
        thumb_side = -1 if name.endswith("l") else 1
        thumb = add_uv_ellipsoid(prefix + f" Thumb {name}", (0, 0, 0), (0.038 * scale, 0.04 * scale, 0.13 * scale), white, 20, 10)
        parent_local(
            thumb,
            points[name],
            (thumb_side * 0.16 * scale, -0.02 * scale, 0.02 * scale),
            (0, thumb_side * math.radians(35), 0),
        )
        objects.append(thumb)
    for name in ("ankle_l", "ankle_r"):
        foot = add_uv_ellipsoid(prefix + " Foot " + name, (0, 0, 0), (0.26 * scale, 0.55 * scale, 0.18 * scale), white, 32, 18)
        foot.parent = points[name]
        foot.location = (0, -0.25 * scale, -0.05 * scale)
        objects.append(foot)
    return points, objects


def add_wings(
    prefix: str,
    center: tuple[float, float, float],
    size: float,
    white: bpy.types.Material,
) -> list[bpy.types.Object]:
    objects: list[bpy.types.Object] = []
    cx, cy, cz = center
    for side in (-1, 1):
        for row in range(3):
            count = 8 - row
            for index in range(count):
                reach = (0.55 + index * 0.36 + row * 0.12) * size
                x = cx + side * reach
                y = cy + 0.20 * row * size
                z = cz + (0.10 + index * 0.18 - row * 0.18) * size
                feather = add_uv_ellipsoid(
                    prefix + f" Feather {side} {row} {index}",
                    (x, y, z),
                    (0.18 * size, 0.07 * size, (0.62 + index * 0.055) * size),
                    white,
                    24,
                    14,
                )
                feather.rotation_euler = (
                    math.radians(5 + row * 7),
                    side * math.radians(48 - index * 2),
                    side * math.radians(7 + row * 4),
                )
                objects.append(feather)
    return objects


def add_static_bust(
    prefix: str,
    center: tuple[float, float, float],
    scale: float,
    white: bpy.types.Material,
    seam: bpy.types.Material,
) -> list[bpy.types.Object]:
    cx, cy, cz = center
    objects: list[bpy.types.Object] = []
    shoulders = add_tapered_torso(
        prefix + " Shoulders",
        (cx, cy, cz),
        3.45 * scale,
        2.30 * scale,
        1.16 * scale,
        1.55 * scale,
        white,
    )
    objects.append(shoulders)
    chest = add_tapered_torso(
        prefix + " Chest",
        (cx, cy, cz - 0.86 * scale),
        2.35 * scale,
        1.70 * scale,
        1.00 * scale,
        1.25 * scale,
        white,
    )
    objects.append(chest)
    head_point = add_empty(prefix + " Head Point", (cx, cy - 0.05 * scale, cz + 1.72 * scale))
    head = add_uv_ellipsoid(prefix + " Head", (0, 0, 0), (0.65 * scale, 0.58 * scale, 0.86 * scale), white)
    copy_location(head, head_point)
    objects.append(head)
    objects.extend(make_face_details(prefix, head_point, 1.42 * scale, white, seam))
    objects.extend(add_wings(prefix + " Wing", (cx, cy + 0.35 * scale, cz + 0.5 * scale), 1.05 * scale, white))
    return objects


def look_at(obj: bpy.types.Object, target: tuple[float, float, float]) -> None:
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def key_camera(
    camera: bpy.types.Object,
    frame: int,
    location: tuple[float, float, float],
    target: tuple[float, float, float],
    lens: float,
) -> None:
    camera.location = location
    look_at(camera, target)
    camera.data.lens = lens
    camera.keyframe_insert(data_path="location", frame=frame)
    camera.keyframe_insert(data_path="rotation_euler", frame=frame)
    camera.data.keyframe_insert(data_path="lens", frame=frame)


def add_area_light(
    name: str,
    location: tuple[float, float, float],
    target: tuple[float, float, float],
    energy: float,
    size: float,
) -> bpy.types.Object:
    data = bpy.data.lights.new(name, "AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    look_at(obj, target)
    return obj


def build_scene(args: argparse.Namespace) -> None:
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = FRAME_END
    scene.render.fps = FPS
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 720
    scene.render.resolution_y = 1280
    scene.render.resolution_percentage = 100 if args.full_resolution else 50
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    if hasattr(scene.render, "use_motion_blur"):
        scene.render.use_motion_blur = False
    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass

    white = make_material("Matte White Clay", (0.82, 0.84, 0.86, 1), 0.32, 0.03)
    seam = make_material("Soft Gray Joints", (0.16, 0.18, 0.20, 1), 0.42, 0.12)
    green = make_material("Chroma Green", (0.002, 0.28, 0.008, 1), 0.42, 0.0)

    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 2.0, 0))
    floor = bpy.context.object
    floor.name = "Green Floor"
    apply_material(floor, green)
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 8.0, 8.0), rotation=(math.radians(90), 0, 0))
    backdrop = bpy.context.object
    backdrop.name = "Green Backdrop"
    apply_material(backdrop, green)
    scene.world.color = (0.008, 0.03, 0.01)

    add_area_light("Key Light", (4.5, -5.5, 8.5), (0, 1.0, 3.0), 1300, 5.5)
    add_area_light("Fill Light", (-4.0, -3.0, 5.5), (0, 1.0, 2.6), 850, 4.5)
    add_area_light("Rim Light", (0, 5.0, 8.0), (0, 1.0, 3.2), 1500, 4.0)

    camera_data = bpy.data.cameras.new("Main Camera")
    camera = bpy.data.objects.new("Main Camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera_data.sensor_width = 36
    camera_data.dof.use_dof = False

    shot1_points, shot1_objects = make_mannequin("Shot1 Hero", (0, 0, 0), 1.0, white, seam)
    shot1_objects.extend(add_wings("Shot1", (0, 0.55, 3.05), 1.0, white))
    animate_point(shot1_points["elbow_l"], [
        (1, (-0.95, -0.45, 2.55)), (30, (-1.00, -0.72, 3.05)),
        (60, (-1.22, -0.62, 3.38)), (90, (-0.88, -0.55, 2.55)),
        (120, (-1.10, -1.20, 3.10)), (132, (-0.80, -1.55, 3.45)),
    ])
    animate_point(shot1_points["wrist_l"], [
        (1, (-0.30, -1.05, 3.55)), (30, (0.22, -1.18, 3.55)),
        (60, (-0.52, -1.26, 4.10)), (90, (-0.12, -1.05, 3.05)),
        (120, (-0.60, -1.85, 3.80)), (132, (0.05, -2.15, 4.15)),
    ])
    animate_point(shot1_points["elbow_r"], [
        (1, (0.95, -0.48, 2.62)), (30, (1.00, -0.68, 3.10)),
        (60, (1.18, -0.66, 3.35)), (90, (0.82, -0.58, 2.62)),
        (120, (1.05, -1.25, 3.02)), (132, (0.70, -1.58, 3.55)),
    ])
    animate_point(shot1_points["wrist_r"], [
        (1, (0.34, -1.12, 3.82)), (30, (-0.25, -1.24, 3.72)),
        (60, (0.55, -1.28, 3.92)), (90, (0.16, -1.10, 2.90)),
        (120, (0.63, -1.88, 3.62)), (132, (-0.05, -2.18, 4.00)),
    ])
    set_render_visibility(shot1_objects, 1, SHOT_CUT - 1)

    shot2_points, shot2_objects = make_mannequin("Shot2 Puppet", (0, -0.3, 0), 0.78, white, seam)
    giant = add_static_bust("Shot2 Giant", (0, 4.2, 2.2), 1.55, white, seam)
    shot2_objects.extend(giant)

    animate_point(shot2_points["elbow_l"], [
        (SHOT_CUT, (-1.05, -0.75, 3.10)), (170, (-1.28, -0.55, 3.42)),
        (210, (-0.95, -0.92, 3.22)), (258, (-1.32, -0.45, 3.58)),
    ])
    animate_point(shot2_points["wrist_l"], [
        (SHOT_CUT, (-1.45, -0.85, 3.92)), (170, (-1.55, -0.65, 4.20)),
        (210, (-1.36, -1.00, 3.86)), (258, (-1.64, -0.52, 4.35)),
    ])
    animate_point(shot2_points["elbow_r"], [
        (SHOT_CUT, (1.03, -0.78, 3.05)), (170, (1.30, -0.58, 3.38)),
        (210, (0.88, -0.95, 3.18)), (258, (1.28, -0.50, 3.52)),
    ])
    animate_point(shot2_points["wrist_r"], [
        (SHOT_CUT, (1.46, -0.88, 3.86)), (170, (1.60, -0.68, 4.14)),
        (210, (1.32, -1.03, 3.80)), (258, (1.60, -0.56, 4.28)),
    ])
    animate_point(shot2_points["knee_l"], [
        (SHOT_CUT, (-0.42, -0.28, 0.72)), (170, (-0.55, -0.55, 0.92)),
        (210, (-0.25, -0.18, 0.56)), (258, (-0.48, -0.62, 0.88)),
    ])
    animate_point(shot2_points["ankle_l"], [
        (SHOT_CUT, (-0.44, -0.25, 0.12)), (170, (-0.72, -0.86, 0.18)),
        (210, (-0.32, -0.18, 0.10)), (258, (-0.70, -0.90, 0.16)),
    ])
    animate_point(shot2_points["knee_r"], [
        (SHOT_CUT, (0.42, -0.25, 0.82)), (170, (0.30, -0.12, 0.62)),
        (210, (0.58, -0.66, 0.96)), (258, (0.28, -0.16, 0.66)),
    ])

    for side, wrist_name, x in (("L", "wrist_l", -1.7), ("R", "wrist_r", 1.7)):
        top = add_empty(f"String Top {side}", (x, -0.6, 6.8))
        string = add_dynamic_segment(f"Puppet String {side}", top, shot2_points[wrist_name], 0.018, white)
        shot2_objects.append(string)
    set_render_visibility(shot2_objects, SHOT_CUT, FRAME_END)

    key_camera(camera, 1, (0.0, -8.8, 3.55), (0, 0, 3.2), 61)
    key_camera(camera, 64, (-0.28, -7.4, 3.72), (0.05, -0.15, 3.32), 66)
    key_camera(camera, 120, (0.35, -6.35, 3.65), (0, -0.25, 3.42), 72)
    key_camera(camera, 132, (0.05, -5.85, 3.78), (0, -0.50, 3.55), 78)
    key_camera(camera, 133, (0.0, -13.4, 3.45), (0, 1.6, 2.75), 48)
    key_camera(camera, 190, (-0.55, -11.0, 3.75), (0, 1.65, 3.00), 54)
    key_camera(camera, 258, (0.40, -9.1, 3.95), (0, 1.8, 3.15), 61)

    args.project.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.project))

    if args.render_tests:
        if args.test_dir is None:
            raise SystemExit("--test-dir is required with --render-tests")
        args.test_dir.mkdir(parents=True, exist_ok=True)
        for frame in (1, 38, 76, 120, 133, 190, 250):
            scene.frame_set(frame)
            scene.render.filepath = str(args.test_dir / f"frame_{frame:04d}.png")
            bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    build_scene(parse_args())
