#!/usr/bin/env python3
"""Read Quake 3 / Urban Terror IBSP 46 maps from BSP or PK3 files.

This module deliberately reads the compiled BSP.  Source ``.map`` or ``.bak``
files are optional inputs in community archives and cannot be the basis of an
automatic converter.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence


IBSP_MAGIC = b"IBSP"
IBSP_VERSION = 46
LUMP_COUNT = 17

L_ENTITIES = 0
L_TEXTURES = 1
L_PLANES = 2
L_BRUSHES = 8
L_BRUSHSIDES = 9
L_VERTICES = 10
L_MESHVERTS = 11
L_FACES = 13

CONTENTS_SOLID = 1
SPAWN_CLEARANCE = 12.0
SPAWN_MAX_DROP = 256.0
MIN_WALKABLE_NORMAL_Z = math.cos(math.radians(50.0))


class BspError(ValueError):
    """Raised for malformed or unsupported BSP data."""


@dataclass(frozen=True)
class Lump:
    offset: int
    length: int


@dataclass(frozen=True)
class Texture:
    name: str
    flags: int
    contents: int


@dataclass(frozen=True)
class Plane:
    normal: tuple[float, float, float]
    distance: float


@dataclass(frozen=True)
class Brush:
    first_side: int
    side_count: int
    texture: int


@dataclass(frozen=True)
class BrushSide:
    plane: int
    texture: int


@dataclass(frozen=True)
class Vertex:
    position: tuple[float, float, float]
    uv: tuple[float, float]


@dataclass(frozen=True)
class Face:
    texture: int
    kind: int
    first_vertex: int
    vertex_count: int
    first_meshvert: int
    meshvert_count: int
    patch_size: tuple[int, int]


@dataclass(frozen=True)
class Spawn:
    classname: str
    team: str
    origin: tuple[float, float, float]
    yaw: float


@dataclass
class BspMap:
    source_name: str
    entities: list[dict[str, str]]
    textures: list[Texture]
    planes: list[Plane]
    brushes: list[Brush]
    brush_sides: list[BrushSide]
    vertices: list[Vertex]
    meshverts: list[int]
    faces: list[Face]

    @classmethod
    def read(cls, data: bytes, source_name: str = "<memory>") -> "BspMap":
        header_size = 8 + LUMP_COUNT * 8
        if len(data) < header_size:
            raise BspError(f"{source_name}: truncated BSP header")
        magic, version = struct.unpack_from("<4si", data)
        if magic != IBSP_MAGIC:
            raise BspError(f"{source_name}: expected IBSP magic, found {magic!r}")
        if version != IBSP_VERSION:
            raise BspError(
                f"{source_name}: unsupported IBSP version {version}; expected 46"
            )
        lumps = [Lump(*values) for values in struct.iter_unpack("<ii", data[8:header_size])]
        for index, lump in enumerate(lumps):
            if lump.offset < 0 or lump.length < 0 or lump.offset + lump.length > len(data):
                raise BspError(f"{source_name}: invalid lump {index}")

        def chunk(index: int) -> bytes:
            lump = lumps[index]
            return data[lump.offset : lump.offset + lump.length]

        textures = [
            Texture(raw_name.split(b"\0", 1)[0].decode("utf-8", "replace"), flags, contents)
            for raw_name, flags, contents in _records(chunk(L_TEXTURES), "<64sii", "texture")
        ]
        planes = [
            Plane((nx, ny, nz), distance)
            for nx, ny, nz, distance in _records(chunk(L_PLANES), "<4f", "plane")
        ]
        brushes = [Brush(*values) for values in _records(chunk(L_BRUSHES), "<3i", "brush")]
        brush_sides = [
            BrushSide(*values)
            for values in _records(chunk(L_BRUSHSIDES), "<2i", "brush side")
        ]
        vertices = [
            Vertex((values[0], values[1], values[2]), (values[3], values[4]))
            for values in _records(chunk(L_VERTICES), "<10f4B", "vertex")
        ]
        meshverts = [values[0] for values in _records(chunk(L_MESHVERTS), "<i", "meshvert")]
        faces = []
        for values in _records(chunk(L_FACES), "<12i12f2i", "face"):
            faces.append(
                Face(
                    texture=values[0],
                    kind=values[2],
                    first_vertex=values[3],
                    vertex_count=values[4],
                    first_meshvert=values[5],
                    meshvert_count=values[6],
                    patch_size=(values[24], values[25]),
                )
            )
        result = cls(
            source_name=source_name,
            entities=_parse_entities(chunk(L_ENTITIES)),
            textures=textures,
            planes=planes,
            brushes=brushes,
            brush_sides=brush_sides,
            vertices=vertices,
            meshverts=meshverts,
            faces=faces,
        )
        result.validate()
        return result

    def validate(self) -> None:
        for index, brush in enumerate(self.brushes):
            if not _range_ok(brush.first_side, brush.side_count, len(self.brush_sides)):
                raise BspError(f"{self.source_name}: brush {index} has invalid sides")
            if not 0 <= brush.texture < len(self.textures):
                raise BspError(f"{self.source_name}: brush {index} has invalid texture")
        for index, side in enumerate(self.brush_sides):
            if not 0 <= side.plane < len(self.planes):
                raise BspError(f"{self.source_name}: brush side {index} has invalid plane")
        for index, face in enumerate(self.faces):
            if not _range_ok(face.first_vertex, face.vertex_count, len(self.vertices)):
                raise BspError(f"{self.source_name}: face {index} has invalid vertices")
            if not _range_ok(face.first_meshvert, face.meshvert_count, len(self.meshverts)):
                raise BspError(f"{self.source_name}: face {index} has invalid meshverts")

    def spawns(self) -> list[Spawn]:
        result = []
        teams = {
            "team_ctf_redspawn": "alpha",
            "team_ctf_redplayer": "alpha",
            "team_ctf_bluespawn": "omega",
            "team_ctf_blueplayer": "omega",
            "info_player_start": "neutral",
            "info_player_deathmatch": "neutral",
        }
        for entity in self.entities:
            classname = entity.get("classname", "").lower()
            if classname not in teams or "origin" not in entity:
                continue
            origin = _float_tuple(entity["origin"], 3)
            yaw = float(entity.get("angle", "0"))
            if "angles" in entity:
                angles = _float_tuple(entity["angles"], 3)
                yaw = angles[1]
            result.append(Spawn(classname, teams[classname], origin, yaw))
        return result

    def solid_brushes(self) -> Iterable[tuple[int, Brush]]:
        for index, brush in enumerate(self.brushes):
            if self.textures[brush.texture].contents & CONTENTS_SOLID:
                yield index, brush

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        if not self.vertices:
            raise BspError(f"{self.source_name}: map has no vertices")
        axes = list(zip(*(vertex.position for vertex in self.vertices)))
        return tuple(min(axis) for axis in axes), tuple(max(axis) for axis in axes)

    def manifest(self) -> dict[str, object]:
        minimum, maximum = self.bounds()
        brushes = []
        for index, brush in self.solid_brushes():
            sides = self.brush_sides[brush.first_side : brush.first_side + brush.side_count]
            brushes.append(
                {
                    "index": index,
                    "texture": self.textures[brush.texture].name,
                    "planes": [
                        {
                            "normal": self.planes[side.plane].normal,
                            "distance": self.planes[side.plane].distance,
                        }
                        for side in sides
                    ],
                }
            )
        return {
            "format": "IBSP",
            "version": IBSP_VERSION,
            "source": self.source_name,
            "bounds": {"minimum": minimum, "maximum": maximum},
            "counts": {
                "entities": len(self.entities),
                "spawns": len(self.spawns()),
                "textures": len(self.textures),
                "brushes": len(self.brushes),
                "solid_brushes": len(brushes),
                "faces": len(self.faces),
                "vertices": len(self.vertices),
            },
            "spawns": [spawn.__dict__ for spawn in self.spawns()],
            "solid_brushes": brushes,
        }

    def write_obj(self, target: BinaryIO, patch_subdivisions: int = 4) -> dict[str, int]:
        """Export render geometry, including tessellated quadratic patches."""
        target.write(f"# Converted from {self.source_name}\n".encode())
        for vertex in self.vertices:
            target.write(("v %.9g %.9g %.9g\n" % _obj_position(vertex.position)).encode())
        for vertex in self.vertices:
            target.write(("vt %.9g %.9g\n" % (vertex.uv[0], 1.0 - vertex.uv[1])).encode())

        vertex_total = len(self.vertices)
        triangle_total = 0
        current_material = None
        for face in self.faces:
            material = _material_name(
                self.textures[face.texture].name if 0 <= face.texture < len(self.textures) else "missing"
            )
            if material != current_material:
                # Red Eclipse's OBJ loader creates meshes from ``g`` records and
                # ignores Wavefront ``usemtl`` records.  Naming each mesh after
                # the Q3 shader lets a generated obj.cfg bind its skin later.
                target.write(f"g {material}\n".encode())
                current_material = material
            if face.kind in (1, 3):
                indices = [
                    face.first_vertex + self.meshverts[face.first_meshvert + offset]
                    for offset in range(face.meshvert_count)
                ]
                if not indices and face.vertex_count >= 3:
                    indices = [
                        item
                        for offset in range(1, face.vertex_count - 1)
                        for item in (face.first_vertex, face.first_vertex + offset, face.first_vertex + offset + 1)
                    ]
                for offset in range(0, len(indices) - 2, 3):
                    tri = indices[offset : offset + 3]
                    if all(0 <= item < len(self.vertices) for item in tri):
                        target.write(("f " + " ".join(f"{item + 1}/{item + 1}" for item in tri) + "\n").encode())
                        triangle_total += 1
            elif face.kind == 2:
                patch_vertices, patch_triangles = self._tessellate_patch(face, patch_subdivisions)
                first = vertex_total + 1
                for position, uv in patch_vertices:
                    target.write(("v %.9g %.9g %.9g\n" % _obj_position(position)).encode())
                    target.write(("vt %.9g %.9g\n" % (uv[0], 1.0 - uv[1])).encode())
                # OBJ has separate global indices for vertices and UVs. Original UVs
                # precede generated UVs, so generated UV index starts at the same value.
                for tri in patch_triangles:
                    target.write(("f " + " ".join(f"{first + item}/{first + item}" for item in tri) + "\n").encode())
                vertex_total += len(patch_vertices)
                triangle_total += len(patch_triangles)
        return {"vertices": vertex_total, "triangles": triangle_total}

    def surface_triangles(
        self, patch_subdivisions: int = 4, solid_only: bool = False
    ) -> Iterable[tuple[tuple[float, float, float], ...]]:
        """Yield render-surface triangles in BSP coordinates.

        Compiled BSP surface geometry is available even when the archive does
        not include source brushes, so it is also the universal collision
        fallback used by the converter.
        """
        for face in self.faces:
            if solid_only and (
                not 0 <= face.texture < len(self.textures)
                or not self.textures[face.texture].contents & CONTENTS_SOLID
            ):
                continue
            if face.kind in (1, 3):
                indices = [
                    face.first_vertex + self.meshverts[face.first_meshvert + offset]
                    for offset in range(face.meshvert_count)
                ]
                if not indices and face.vertex_count >= 3:
                    indices = [
                        item
                        for offset in range(1, face.vertex_count - 1)
                        for item in (
                            face.first_vertex,
                            face.first_vertex + offset,
                            face.first_vertex + offset + 1,
                        )
                    ]
                for offset in range(0, len(indices) - 2, 3):
                    tri = indices[offset : offset + 3]
                    if all(0 <= item < len(self.vertices) for item in tri):
                        yield tuple(self.vertices[item].position for item in tri)
            elif face.kind == 2:
                vertices, triangles = self._tessellate_patch(face, patch_subdivisions)
                for tri in triangles:
                    yield tuple(vertices[item][0] for item in tri)

    def write_collision_obj(
        self, target: BinaryIO, patch_subdivisions: int = 4
    ) -> dict[str, int]:
        """Write a two-sided collision mesh independent from render winding."""
        target.write(f"# Two-sided collision for {self.source_name}\ng collision\n".encode())
        triangle_total = 0
        triangles = list(self.surface_triangles(patch_subdivisions, solid_only=True))
        if not triangles:
            triangles = list(self.surface_triangles(patch_subdivisions))
        for triangle in triangles:
            first = triangle_total * 3 + 1
            for position in triangle:
                target.write(("v %.9g %.9g %.9g\n" % _obj_position(position)).encode())
            target.write(f"f {first} {first + 1} {first + 2}\n".encode())
            target.write(f"f {first + 2} {first + 1} {first}\n".encode())
            triangle_total += 1
        return {"vertices": triangle_total * 3, "triangles": triangle_total * 2}

    def supported_spawns(
        self, floor_policy: str = "nearest"
    ) -> tuple[list[tuple[Spawn, float]], list[Spawn]]:
        """Find a walkable surface below each spawn and return its floor Z."""
        if floor_policy not in ("nearest", "lowest"):
            raise BspError(f"unknown spawn floor policy: {floor_policy}")
        triangles = list(self.surface_triangles(solid_only=True))
        if not triangles:
            triangles = list(self.surface_triangles())
        supported = []
        unsupported = []
        for spawn in self.spawns():
            floor = _support_floor(spawn.origin, triangles, floor_policy)
            if floor is None:
                unsupported.append(spawn)
            else:
                supported.append((spawn, floor))
        return supported, unsupported

    def _tessellate_patch(
        self, face: Face, subdivisions: int
    ) -> tuple[list[tuple[tuple[float, float, float], tuple[float, float]]], list[tuple[int, int, int]]]:
        width, height = face.patch_size
        if subdivisions < 1 or width < 3 or height < 3 or width % 2 == 0 or height % 2 == 0:
            return [], []
        controls = self.vertices[face.first_vertex : face.first_vertex + face.vertex_count]
        if len(controls) < width * height:
            return [], []
        output = []
        triangles = []
        for py in range(0, height - 2, 2):
            for px in range(0, width - 2, 2):
                base = len(output)
                patch = [[controls[(py + y) * width + px + x] for x in range(3)] for y in range(3)]
                for y in range(subdivisions + 1):
                    v = y / subdivisions
                    for x in range(subdivisions + 1):
                        u = x / subdivisions
                        position = tuple(
                            _bezier2([patch[row][col].position[axis] for row in range(3) for col in range(3)], u, v)
                            for axis in range(3)
                        )
                        uv = tuple(
                            _bezier2([patch[row][col].uv[axis] for row in range(3) for col in range(3)], u, v)
                            for axis in range(2)
                        )
                        output.append((position, uv))
                stride = subdivisions + 1
                for y in range(subdivisions):
                    for x in range(subdivisions):
                        a = base + y * stride + x
                        b, c, d = a + 1, a + stride + 1, a + stride
                        triangles.extend(((a, b, c), (a, c, d)))
        return output, triangles


def read_maps(path: Path) -> list[BspMap]:
    if path.suffix.lower() == ".bsp":
        return [BspMap.read(path.read_bytes(), path.name)]
    if path.suffix.lower() not in (".pk3", ".zip"):
        raise BspError(f"{path}: expected a .pk3, .zip or .bsp file")
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                name for name in archive.namelist() if name.lower().startswith("maps/") and name.lower().endswith(".bsp")
            )
            if not names:
                raise BspError(f"{path}: archive contains no maps/*.bsp")
            return [BspMap.read(archive.read(name), f"{path.name}:{name}") for name in names]
    except zipfile.BadZipFile as error:
        raise BspError(f"{path}: invalid PK3/ZIP archive") from error


def write_eclipse_stage(
    source: Path,
    bsp: BspMap,
    stage: Path,
    scale: float = 0.25,
    spawn_floor: str = "nearest",
) -> dict[str, object]:
    """Create model assets and a CubeScript job that saves a native MPZ.

    The generated map uses the imported render mesh as a triangle-collision
    mapmodel.  This is intentionally separate from a future editable-octree
    backend, but is sufficient for an automated first playability pass.
    """
    if scale <= 0:
        raise BspError("Eclipse scale must be positive")
    stem = Path(bsp.source_name.split(":")[-1]).stem
    model_rel = Path("csgopen") / "imported" / stem
    model_dir = stage / "data" / model_rel
    maps_dir = stage / "data" / "maps"
    profile_dir = stage / "profile"
    model_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    obj_path = model_dir / f"{stem}.obj"
    with obj_path.open("wb") as output:
        mesh_stats = bsp.write_obj(output)

    collision_rel = model_rel / "collision"
    collision_dir = stage / "data" / collision_rel
    collision_dir.mkdir(parents=True, exist_ok=True)
    with (collision_dir / "collision.obj").open("wb") as output:
        collision_stats = bsp.write_collision_obj(output)
    (collision_dir / "obj.cfg").write_text(
        f'objload "collision.obj"\nmdlscale {scale * 100:.9g}\nmdlcullface 0\n',
        encoding="utf-8",
    )

    texture_bindings = _extract_texture_bindings(source, bsp, model_dir)
    model_config = [
        f'objload "{stem}.obj"',
        "mdlcullface 0",
        f"mdlscale {scale * 100:.9g}",
        f'mdltricollide "{collision_rel.as_posix()}"',
    ]
    for material, image in texture_bindings.items():
        model_config.insert(1, f'objskin "{material}" "{image}"')
    (model_dir / "obj.cfg").write_text("\n".join(model_config) + "\n", encoding="utf-8")
    (maps_dir / f"{stem}.cfg").write_text(
        f'// Generated from {bsp.source_name}\nmapmodel "{model_rel.as_posix()}"\n',
        encoding="utf-8",
    )

    minimum, maximum = bsp.bounds()
    extents = [(maximum[index] - minimum[index]) * scale for index in range(3)]
    margin = 64.0
    required = max(extents[0] + 2 * margin, extents[1] + 2 * margin, extents[2] + 2 * margin)
    world_scale = max(10, min(16, math.ceil(math.log2(max(required, 1.0)))))
    world_size = 1 << world_scale
    offset = (
        margin - minimum[0] * scale,
        margin - minimum[1] * scale,
        world_size / 2 + margin - minimum[2] * scale,
    )

    commands = [
        "q3import_done = 0",
        "q3import_finish = [",
        "    if (= $q3import_done 0) [",
        "        q3import_done = 1",
        "        edittoggle",
        f"        newmap {world_scale} {stem}",
        "        mapmodelreset 0",
        f'        exec "maps/{stem}.cfg"',
        "        newent mapmodel 0 0 0 0 100 100",
        "        entpos %.9g %.9g %.9g" % offset,
    ]
    supported_spawns, unsupported_spawns = bsp.supported_spawns(spawn_floor)
    spawn_entries: list[tuple[Spawn, float]]
    if supported_spawns:
        spawn_entries = supported_spawns
    else:
        # Some unusual BSPs keep all collision in non-rendered structures. In
        # that case retaining the authored origins is safer than producing a
        # map with no playerstarts, and the manifest exposes the fallback.
        spawn_entries = [(spawn, spawn.origin[2]) for spawn in bsp.spawns()]
    for spawn_id, (spawn, floor_z) in enumerate(spawn_entries):
        team = {"neutral": 0, "alpha": 1, "omega": 2}[spawn.team]
        position = (
            spawn.origin[0] * scale + offset[0],
            spawn.origin[1] * scale + offset[1],
            floor_z * scale + offset[2] + SPAWN_CLEARANCE,
        )
        commands.extend(
            (
                f"        newent playerstart {team} {round(spawn.yaw) % 360} 0 0 0 {spawn_id} 0",
                "        entpos %.9g %.9g %.9g" % position,
            )
        )
    commands.extend(
        (
            f'        maptitle "Imported {stem}"',
            '        mapauthor "Converted automatically from an Urban Terror PK3"',
            f'        savemap "{stem}"',
            f'        echo "Q3IMPORT_DONE {stem}"',
            "        sleep 1000 [quit]",
            "    ]",
            "]",
            f'edit "{stem}"',
            "sleep 15000 [q3import_finish]",
        )
    )
    (profile_dir / "build-map.cfg").write_text("\n".join(commands) + "\n", encoding="utf-8")
    (profile_dir / "play.cfg").write_text(
        "\n".join(
            (
                'exec "config/csgopen/tdm.cfg"',
                'exec "config/csgopen/client.cfg"',
                f"tdm {stem}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    conversion = {
        "map": stem,
        "model": model_rel.as_posix(),
        "scale": scale,
        "world_scale": world_scale,
        "world_size": world_size,
        "offset": offset,
        "textures_extracted": len(texture_bindings),
        "render_mesh": mesh_stats,
        "collision_mesh": collision_stats,
        "spawns_written": len(spawn_entries),
        "spawns_supported": len(supported_spawns),
        "spawns_unsupported": len(unsupported_spawns),
        "spawn_floor_policy": spawn_floor,
        "spawn_fallback": not supported_spawns and bool(bsp.spawns()),
        "expected_mpz": str(profile_dir / "maps" / f"{stem}.mpz"),
    }
    (stage / f"{stem}.json").write_text(
        json.dumps({**bsp.manifest(), "eclipse": conversion}, indent=2) + "\n",
        encoding="utf-8",
    )
    return conversion


def _extract_texture_bindings(source: Path, bsp: BspMap, model_dir: Path) -> dict[str, str]:
    if source.suffix.lower() not in (".pk3", ".zip"):
        return {}
    bindings = {}
    with zipfile.ZipFile(source) as archive:
        entries = {name.lower(): name for name in archive.namelist()}
        for texture in bsp.textures:
            match = None
            for extension in (".png", ".jpg", ".jpeg", ".tga"):
                match = entries.get(texture.name.lower() + extension)
                if match:
                    break
            if not match:
                continue
            extension = Path(match).suffix.lower()
            filename = _material_name(texture.name) + extension
            with archive.open(match) as input_file, (model_dir / filename).open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
            bindings[_material_name(texture.name)] = filename
    return bindings


def _records(data: bytes, fmt: str, label: str) -> Iterable[tuple[object, ...]]:
    size = struct.calcsize(fmt)
    if len(data) % size:
        raise BspError(f"malformed {label} lump ({len(data)} bytes is not a multiple of {size})")
    return struct.iter_unpack(fmt, data)


def _range_ok(first: int, count: int, total: int) -> bool:
    return first >= 0 and count >= 0 and first + count <= total


def _parse_entities(data: bytes) -> list[dict[str, str]]:
    text = data.rstrip(b"\0").decode("utf-8", "replace")
    result = []
    for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL):
        pairs = re.findall(r'"((?:\\.|[^"\\])*)"\s*"((?:\\.|[^"\\])*)"', body)
        result.append({_unescape_entity(key): _unescape_entity(value) for key, value in pairs})
    return result


def _unescape_entity(value: str) -> str:
    # Quake entity strings only need quote/backslash unescaping.  Using Python's
    # unicode_escape codec would incorrectly treat map paths such as ``\ut4`` as
    # truncated Unicode escapes.
    return value.replace('\\"', '"').replace('\\\\', '\\')


def _float_tuple(value: str, count: int) -> tuple[float, ...]:
    parts = value.split()
    if len(parts) != count:
        raise BspError(f"expected {count} coordinates, found {value!r}")
    return tuple(float(part) for part in parts)


def _obj_position(position: tuple[float, float, float]) -> tuple[float, float, float]:
    """Compensate for Eclipse's OBJ-to-engine ``(z, -x, y)`` transform."""
    x, y, z = position
    return -y, z, x


def _support_floor(
    origin: tuple[float, float, float],
    triangles: Iterable[tuple[tuple[float, float, float], ...]],
    floor_policy: str = "nearest",
) -> float | None:
    """Return the closest walkable triangle below a point."""
    best = None
    for triangle in triangles:
        a, b, c = triangle
        ab = tuple(b[index] - a[index] for index in range(3))
        ac = tuple(c[index] - a[index] for index in range(3))
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        magnitude = math.sqrt(sum(value * value for value in normal))
        if magnitude == 0 or abs(normal[2]) / magnitude < MIN_WALKABLE_NORMAL_Z:
            continue
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-9:
            continue
        u = ((b[1] - c[1]) * (origin[0] - c[0]) + (c[0] - b[0]) * (origin[1] - c[1])) / denominator
        v = ((c[1] - a[1]) * (origin[0] - c[0]) + (a[0] - c[0]) * (origin[1] - c[1])) / denominator
        w = 1.0 - u - v
        if min(u, v, w) < -1e-6:
            continue
        z = u * a[2] + v * b[2] + w * c[2]
        drop = origin[2] - z
        if -8.0 <= drop <= SPAWN_MAX_DROP:
            if best is None or (floor_policy == "lowest" and z < best) or (floor_policy == "nearest" and z > best):
                best = z
    return best


def _bezier2(values: Sequence[float], u: float, v: float) -> float:
    bu = ((1 - u) ** 2, 2 * u * (1 - u), u**2)
    bv = ((1 - v) ** 2, 2 * v * (1 - v), v**2)
    return sum(values[y * 3 + x] * bu[x] * bv[y] for y in range(3) for x in range(3))


def _material_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "missing"


def _select_map(maps: list[BspMap], requested: str | None) -> BspMap:
    if requested is None:
        if len(maps) != 1:
            available = ", ".join(item.source_name.rsplit("/", 1)[-1] for item in maps)
            raise BspError(f"archive contains multiple BSP maps; use --map ({available})")
        return maps[0]
    requested = requested.lower().removesuffix(".bsp")
    matches = [item for item in maps if Path(item.source_name.split(":")[-1]).stem.lower() == requested]
    if len(matches) != 1:
        raise BspError(f"map {requested!r} was not found uniquely in the archive")
    return matches[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Urban Terror/Quake 3 .pk3 or IBSP 46 .bsp")
    parser.add_argument("output", type=Path, nargs="?", help="output directory (writes OBJ and JSON)")
    parser.add_argument("--map", help="BSP stem when the archive contains multiple maps")
    parser.add_argument("--patch-subdivisions", type=int, default=4)
    parser.add_argument(
        "--eclipse-stage",
        action="store_true",
        help="write a staged Eclipse Recoil model, map config and MPZ build job",
    )
    parser.add_argument("--eclipse-scale", type=float, default=0.25)
    parser.add_argument(
        "--spawn-floor",
        choices=("nearest", "lowest"),
        default="nearest",
        help="surface selection below player starts (default: nearest)",
    )
    args = parser.parse_args(argv)
    try:
        bsp = _select_map(read_maps(args.source), args.map)
        manifest = bsp.manifest()
        if args.output is None:
            summary = dict(manifest)
            summary.pop("solid_brushes")
            print(json.dumps(summary, indent=2))
            return 0
        if args.eclipse_stage:
            conversion = write_eclipse_stage(
                args.source, bsp, args.output, args.eclipse_scale, args.spawn_floor
            )
            print(f"Wrote Eclipse Recoil stage to {args.output}")
            print(f"Expected generated map: {conversion['expected_mpz']}")
            print(
                "Spawn support: "
                f"{conversion['spawns_supported']} supported, "
                f"{conversion['spawns_unsupported']} unsupported"
            )
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        stem = Path(bsp.source_name.split(":")[-1]).stem
        obj_path = args.output / f"{stem}.obj"
        with obj_path.open("wb") as output:
            manifest["render_mesh"] = bsp.write_obj(output, args.patch_subdivisions)
        manifest_path = args.output / f"{stem}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {obj_path}")
        print(f"Wrote {manifest_path}")
        return 0
    except (BspError, OSError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
