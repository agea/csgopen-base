#!/usr/bin/env python3
"""Decode Source 1 static props with Blender + Plumber into Eclipse OBJ tiles.

Run this script through Blender's ``--background --python`` entry point.  The
world BSP converter consumes the emitted manifest and places every generated
tile at the same world offset as the compiled BSP mesh.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


MAX_TRIANGLES_PER_MODEL = 12_000
PROP_TILE_SIZE = 1024.0


def _arguments() -> argparse.Namespace:
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bsp", required=True, type=Path)
    parser.add_argument("--vpk", required=True, type=Path)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--plumber", required=True, type=Path)
    parser.add_argument("--triangle-budget", type=int, default=300_000)
    parser.add_argument("--scale", type=float, default=0.25)
    return parser.parse_args(arguments)


@dataclass
class LocalTriangle:
    material: str
    points: tuple[tuple[float, float, float], ...]
    uvs: tuple[tuple[float, float], ...]


@dataclass
class ModelGeometry:
    triangles: list[LocalTriangle]


@dataclass
class ObjPart:
    directory: Path
    relative: str
    pending: list[object] = field(default_factory=list)
    triangles: int = 0
    bindings: dict[str, str] = field(default_factory=dict)
    sealed: bool = False

    @property
    def vertices(self) -> int:
        return self.triangles * 3

    def add(self, triangle) -> None:
        self.pending.append(triangle)
        self.triangles += 1

    def seal(self, sourcebsp) -> None:
        if self.sealed:
            return
        with (self.directory / "props.obj").open("wb") as output:
            _stats, material_meshes = sourcebsp._write_render_obj(output, self.pending, "static props")
        self.bindings = {
            mesh: material
            for material, meshes in material_meshes.items()
            for mesh in meshes
        }
        self.pending.clear()
        self.sealed = True

    def close(self, sourcebsp, texture_names: dict[str, str], scale: float) -> None:
        self.seal(sourcebsp)
        config = ['objload "props.obj"', "mdlcullface 0", f"mdlscale {scale * 100:.9g}", "mdlcollide 0"]
        for mesh, material in sorted(self.bindings.items()):
            texture = texture_names.get(material)
            if texture:
                config.insert(1, f'objskin "{mesh}" "../textures/{texture}"')
        (self.directory / "obj.cfg").write_text("\n".join(config) + "\n", encoding="utf-8")


class TileWriter:
    def __init__(self, root: Path, model_root: str, sourcebsp):
        self.root = root
        self.model_root = model_root
        self.sourcebsp = sourcebsp
        self.parts: list[ObjPart] = []
        self.active: dict[tuple[int, int], ObjPart] = {}
        self.part_counts: Counter[tuple[int, int]] = Counter()

    def add(self, tile: tuple[int, int], triangle, material: str) -> None:
        part = self.active.get(tile)
        if part is None or part.triangles >= MAX_TRIANGLES_PER_MODEL:
            if part is not None:
                part.seal(self.sourcebsp)
            index = self.part_counts[tile]
            self.part_counts[tile] += 1
            suffix = "_".join(str(value).replace("-", "n") for value in tile)
            name = f"tile_{suffix}_{index}"
            directory = self.root / name
            directory.mkdir(parents=True, exist_ok=True)
            part = ObjPart(directory, f"{self.model_root}/{name}")
            self.parts.append(part)
            self.active[tile] = part
        part.add(triangle)


def _source_matrix(angles: tuple[float, float, float]) -> Matrix:
    pitch, yaw, roll = (math.radians(value) for value in angles)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sr, cr = math.sin(roll), math.cos(roll)
    return Matrix(
        (
            (cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
            (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy),
            (-sp, sr * cp, cr * cp),
        )
    )


def _mesh_objects(root):
    objects = [root, *root.children_recursive]
    return [obj for obj in objects if obj.type == "MESH"]


def _protected_component_count(mesh) -> int:
    """Count disconnected islands large enough to expose visible wall holes."""
    polygons = mesh.polygons
    if not polygons:
        return 0
    parents = list(range(len(polygons)))

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    owners = {}
    for polygon in polygons:
        for vertex in polygon.vertices:
            owner = owners.setdefault(vertex, polygon.index)
            union(owner, polygon.index)
    areas = defaultdict(float)
    for polygon in polygons:
        areas[find(polygon.index)] += polygon.area
    # Source models use Source units.  Islands smaller than 16x32 units are
    # typically bars, bolts, leaves, or trim rather than architectural panels.
    return sum(area >= 512.0 for area in areas.values())


def _append_object_geometry(output, root, original, ratio: float, minimum_triangles: int) -> None:
    modifier = None
    polygon_count = len(original.data.polygons)
    if ratio < 0.999 and polygon_count > 3:
        modifier = original.modifiers.new("csgopen_simplify", "DECIMATE")
        modifier.ratio = max(ratio, min(1.0, minimum_triangles / polygon_count))
        bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = original.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        uv_data = mesh.uv_layers.active.data if mesh.uv_layers.active else None
        local_matrix = root.matrix_world.inverted() @ original.matrix_world
        materials = [
            (material.get("path_id") or material.name) if material else "missing"
            for material in original.data.materials
        ]
        for polygon in mesh.loop_triangles:
            material = materials[polygon.material_index] if polygon.material_index < len(materials) else "missing"
            points = tuple(tuple(local_matrix @ mesh.vertices[index].co) for index in polygon.vertices)
            uvs = tuple(tuple(uv_data[loop].uv) if uv_data else (0.0, 0.0) for loop in polygon.loops)
            output.append(LocalTriangle(material, points, uvs))
    finally:
        evaluated.to_mesh_clear()
        if modifier is not None:
            original.modifiers.remove(modifier)


def _geometry(root, ratio: float, structural: bool) -> ModelGeometry:
    output = []
    for original in _mesh_objects(root):
        # Preserve a reserve for every sizeable disconnected architectural
        # island.  Tiny bars, bolts, and leaves do not consume the reserve, so
        # the model remains light while wall/door panels cannot all disappear.
        components = _protected_component_count(original.data) if structural else 1
        minimum_triangles = max(24, components * 8)
        _append_object_geometry(output, root, original, ratio, minimum_triangles)
    return ModelGeometry(output)


def _transform(triangle: LocalTriangle, prop, matrix: Matrix):
    points = []
    for point in triangle.points:
        transformed = matrix @ (Vector(point) * prop.scale)
        points.append(tuple(transformed[index] + prop.origin[index] for index in range(3)))
    return tuple(points)


def _extract_pak_models(bsp, models: set[str], destination: Path) -> None:
    stems = {model.lower().removesuffix(".mdl") for model in models}
    if not bsp.pakfile:
        return
    with zipfile.ZipFile(io.BytesIO(bsp.pakfile)) as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/").lower()
            if any(normalized == stem + extension for stem in stems for extension in (".mdl", ".vvd", ".dx90.vtx", ".phy")):
                target = destination / normalized
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))


def main() -> int:
    args = _arguments()
    scripts = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts))
    sys.path.insert(0, str(args.plumber.resolve().parent))
    import sourcebsp
    from plumber.asset import AssetCallbacks
    from plumber.plumber import FileSystem, Importer

    bsp = sourcebsp.SourceBsp.read(args.bsp.read_bytes(), args.bsp.name)
    bounds = bsp.playable_bounds()
    props = [
        prop
        for prop in bsp.static_props
        if not bounds
        or all(bounds[0][axis] - 512 <= prop.origin[axis] <= bounds[1][axis] + 512 for axis in range(3))
    ]
    usage = Counter(prop.model.lower() for prop in props)
    models = set(usage)
    structural_keywords = (
        "autocombine",
        "arch",
        "column",
        "curb",
        "door",
        "fence",
        "floor",
        "gate",
        "kasbah",
        "platform",
        "railing",
        "roof",
        "scaffold",
        "stairs",
        "trim",
        "wall",
        "window",
    )
    structural_models = {
        prop.model.lower()
        for prop in props
        if prop.solid or any(keyword in prop.model.lower() for keyword in structural_keywords)
    }
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    with tempfile.TemporaryDirectory(prefix="csgopen-source-pak-") as temporary:
        extracted = Path(temporary)
        _extract_pak_models(bsp, models, extracted)
        filesystem = FileSystem("CS:GO", [("DIR", str(extracted)), ("VPK", str(args.vpk))])
        callbacks = AssetCallbacks(bpy.context)
        imported = {}
        failures = {}
        weighted_triangles = 0
        structural_weighted_triangles = 0
        for number, model in enumerate(sorted(models), 1):
            try:
                importer = Importer(filesystem, callbacks, 4, import_materials=False)
                importer.import_mdl(model, True, import_animations=False)
                root = callbacks.model_tracker.get_last_imported()
                if root is None:
                    raise RuntimeError("decoder returned no model")
                triangles = sum(len(obj.data.polygons) for obj in _mesh_objects(root))
                imported[model] = (root, triangles)
                weighted_triangles += triangles * usage[model]
                if model in structural_models:
                    structural_weighted_triangles += triangles * usage[model]
            except (OSError, RuntimeError) as error:
                failures[model] = str(error)
            if number % 100 == 0:
                print(f"[csgopen] decoded {number}/{len(models)} prop models", flush=True)

        ratio = min(1.0, args.triangle_budget / max(weighted_triangles, 1))
        stem = args.bsp.stem
        model_root = f"csgopen/imported/{stem}/props"
        output_root = args.stage / "data" / model_root
        output_root.mkdir(parents=True, exist_ok=True)
        writer = TileWriter(output_root, model_root, sourcebsp)
        geometry_cache = {}
        written_props = 0
        for number, prop in enumerate(props, 1):
            item = imported.get(prop.model.lower())
            if item is None:
                continue
            if prop.model.lower() not in geometry_cache:
                original_triangles = item[1]
                minimum_ratio = min(1.0, 24 / max(original_triangles, 1))
                geometry_cache[prop.model.lower()] = _geometry(
                    item[0],
                    max(ratio, minimum_ratio),
                    prop.model.lower() in structural_models,
                )
            geometry = geometry_cache[prop.model.lower()]
            transform = _source_matrix(prop.angles)
            tile = (math.floor(prop.origin[0] / PROP_TILE_SIZE), math.floor(prop.origin[1] / PROP_TILE_SIZE))
            for triangle in geometry.triangles:
                points = _transform(triangle, prop, transform)
                writer.add(tile, sourcebsp.Triangle(triangle.material, points, triangle.uvs), triangle.material)
            written_props += 1
            if number % 250 == 0:
                print(f"[csgopen] instanced {number}/{len(props)} static props", flush=True)

        for part in writer.parts:
            part.seal(sourcebsp)
        materials = {material for part in writer.parts for material in part.bindings.values()}
        texture_dir = output_root / "textures"
        texture_dir.mkdir(parents=True, exist_ok=True)
        content = sourcebsp.ContentStore(bsp.pakfile, sourcebsp.VpkArchive(args.vpk))
        dummy_meshes = {material: [material] for material in materials}
        texture_names = {}
        texture_stats = sourcebsp._extract_materials(content, dummy_meshes, texture_dir, texture_names)
        for part in writer.parts:
            part.close(sourcebsp, texture_names, args.scale)

    manifest = {
        "models": [part.relative for part in writer.parts],
        "counts": {
            "source_props": len(bsp.static_props),
            "playable_props": len(props),
            "written_props": written_props,
            "source_models": len(models),
            "decoded_models": len(imported),
            "failed_models": len(failures),
            "source_weighted_triangles": weighted_triangles,
            "source_structural_triangles": structural_weighted_triangles,
            "structural_models": len(structural_models),
            "triangles": sum(part.triangles for part in writer.parts),
            "vertices": sum(part.vertices for part in writer.parts),
            "tile_models": len(writer.parts),
            "simplification_ratio": ratio,
        },
        "textures": texture_stats,
        "failures": failures,
    }
    manifest_path = args.stage / f"{args.bsp.stem}-props.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
