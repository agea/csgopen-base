#!/usr/bin/env python3
"""Convert Source 1 VBSP maps into staged Eclipse Recoil map packages.

The converter reads compiled world surfaces and displacements directly.  It
does not depend on a decompiled VMF, and it can resolve Source materials from
the BSP pakfile plus an optional game VPK directory.
"""

from __future__ import annotations

import argparse
import io
import itertools
import json
import math
import re
import struct
import sys
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

from q3bsp import BspError, SPAWN_CLEARANCE, _obj_position as _engine_obj_position, _support_floor


VBSP_MAGIC = b"VBSP"
SUPPORTED_VERSIONS = {20, 21}
L_ENTITIES = 0
L_PLANES = 1
L_TEXDATA = 2
L_VERTICES = 3
L_NODES = 5
L_TEXINFO = 6
L_FACES = 7
L_LEAVES = 10
L_EDGES = 12
L_SURFEDGES = 13
L_MODELS = 14
L_LEAFBRUSHES = 17
L_BRUSHES = 18
L_BRUSHSIDES = 19
L_DISPINFO = 26
L_DISPVERTS = 33
L_GAME_LUMP = 35
L_PAKFILE = 40
L_TEXDATA_STRING_DATA = 43
L_TEXDATA_STRING_TABLE = 44
LUMP_COUNT = 64
FACE_FORMAT = "<HBBihhhh4Bif2i2iiHHI"
FACE_SIZE = struct.calcsize(FACE_FORMAT)
DISPINFO_SIZE = 176
DISPVERT_SIZE = 20
VPK_SIGNATURE = 0x55AA1234
VPK_DIR_INDEX = 0x7FFF
SURF_SKY = 0x0004
SURF_WARP = 0x0008
SURF_NODRAW = 0x0080
CONTENTS_SOLID = 0x00000001
CONTENTS_WATER = 0x00000020
CONTENTS_PLAYERCLIP = 0x00010000
SOURCE_TOOL_PREFIX = "tools/"
TRIANGLES_PER_MESH = 100
COLLISION_TRIANGLES_PER_MESH = 1000
COLLISION_TILE_SIZE = 1024.0
OBJ_INDEX_LIMIT = 0xFFFF


class SourceBspError(ValueError):
    pass


def _target_position(position: tuple[float, float, float]) -> tuple[float, float, float]:
    """Reflect Source X so imported maps retain their expected left/right layout."""
    return -position[0], position[1], position[2]


def _obj_position(position: tuple[float, float, float]) -> tuple[float, float, float]:
    return _engine_obj_position(_target_position(position))


@dataclass(frozen=True)
class Lump:
    offset: int
    length: int
    version: int
    fourcc: bytes


@dataclass(frozen=True)
class TexData:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class TexInfo:
    uaxis: tuple[float, float, float, float]
    vaxis: tuple[float, float, float, float]
    flags: int
    texdata: int


@dataclass(frozen=True)
class Plane:
    normal: tuple[float, float, float]
    distance: float


@dataclass(frozen=True)
class Brush:
    first_side: int
    side_count: int
    contents: int


@dataclass(frozen=True)
class BrushSide:
    plane: int
    texinfo: int
    bevel: bool


@dataclass(frozen=True)
class BspModel:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]
    origin: tuple[float, float, float]
    headnode: int
    first_face: int
    face_count: int


@dataclass(frozen=True)
class Face:
    plane: int
    first_edge: int
    edge_count: int
    texinfo: int
    dispinfo: int


@dataclass(frozen=True)
class DispInfo:
    start: tuple[float, float, float]
    first_vertex: int
    power: int


@dataclass(frozen=True)
class DispVertex:
    direction: tuple[float, float, float]
    distance: float


@dataclass(frozen=True)
class Spawn:
    classname: str
    team: str
    origin: tuple[float, float, float]
    yaw: float


@dataclass(frozen=True)
class StaticProp:
    model: str
    origin: tuple[float, float, float]
    angles: tuple[float, float, float]
    solid: int
    flags: int
    skin: int
    scale: float


@dataclass(frozen=True)
class Triangle:
    material: str
    points: tuple[tuple[float, float, float], ...]
    uvs: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class MaterialVolume:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]


@dataclass(frozen=True)
class VpkEntry:
    crc: int
    preload: bytes
    archive_index: int
    offset: int
    length: int


class VpkArchive:
    """Small read-only Valve VPK v1/v2 reader."""

    def __init__(self, directory_file: Path):
        self.path = directory_file
        self.entries: dict[str, VpkEntry] = {}
        data = directory_file.read_bytes()
        if len(data) < 12:
            raise SourceBspError(f"{directory_file}: truncated VPK header")
        signature, version, tree_size = struct.unpack_from("<III", data)
        if signature != VPK_SIGNATURE or version not in (1, 2):
            raise SourceBspError(f"{directory_file}: unsupported VPK header")
        header_size = 12 if version == 1 else 28
        if len(data) < header_size + tree_size:
            raise SourceBspError(f"{directory_file}: truncated VPK directory tree")
        self.data_offset = header_size + tree_size
        self._directory_data = data
        stream = io.BytesIO(data[header_size : header_size + tree_size])
        while extension := _read_cstring(stream):
            while directory := _read_cstring(stream):
                while filename := _read_cstring(stream):
                    raw = stream.read(18)
                    if len(raw) != 18:
                        raise SourceBspError(f"{directory_file}: truncated VPK entry")
                    crc, preload_size, archive, offset, length, terminator = struct.unpack("<IHHIIH", raw)
                    if terminator != 0xFFFF:
                        raise SourceBspError(f"{directory_file}: invalid VPK entry terminator")
                    preload = stream.read(preload_size)
                    directory = "" if directory == " " else directory
                    name = "/".join(filter(None, (directory, filename + "." + extension))).lower()
                    self.entries[name] = VpkEntry(crc, preload, archive, offset, length)

    def read(self, name: str) -> bytes | None:
        entry = self.entries.get(name.replace("\\", "/").lower())
        if entry is None:
            return None
        if entry.archive_index == VPK_DIR_INDEX:
            payload = self._directory_data[
                self.data_offset + entry.offset : self.data_offset + entry.offset + entry.length
            ]
        else:
            archive_path = self.path.with_name(
                re.sub(r"_dir\.vpk$", f"_{entry.archive_index:03d}.vpk", self.path.name, flags=re.I)
            )
            with archive_path.open("rb") as archive:
                archive.seek(entry.offset)
                payload = archive.read(entry.length)
        result = entry.preload + payload
        if len(result) != len(entry.preload) + entry.length:
            raise SourceBspError(f"{self.path}: truncated data for {name}")
        if zlib.crc32(result) & 0xFFFFFFFF != entry.crc:
            raise SourceBspError(f"{self.path}: CRC mismatch for {name}")
        return result


class ContentStore:
    def __init__(self, pakfile: bytes, vpk: VpkArchive | None):
        self.vpk = vpk
        self.pak: dict[str, bytes] = {}
        if pakfile:
            with zipfile.ZipFile(io.BytesIO(pakfile)) as archive:
                for name in archive.namelist():
                    self.pak[name.replace("\\", "/").lower()] = archive.read(name)

    def read(self, name: str) -> bytes | None:
        normalized = name.replace("\\", "/").lower()
        data = self.pak.get(normalized)
        return data if data is not None else self.vpk.read(normalized) if self.vpk else None


@dataclass
class SourceBsp:
    source_name: str
    version: int
    entities: list[dict[str, str]]
    vertices: list[tuple[float, float, float]]
    edges: list[tuple[int, int]]
    surfedges: list[int]
    faces: list[Face]
    texinfo: list[TexInfo]
    texdata: list[TexData]
    planes: list[Plane]
    brushes: list[Brush]
    brushsides: list[BrushSide]
    models: list[BspModel]
    model_brushes: list[tuple[int, ...]]
    dispinfo: list[DispInfo]
    dispverts: list[DispVertex]
    static_props: list[StaticProp]
    pakfile: bytes

    @classmethod
    def read(cls, data: bytes, source_name: str = "<memory>") -> "SourceBsp":
        if len(data) < 8 + LUMP_COUNT * 16 + 4:
            raise SourceBspError(f"{source_name}: truncated VBSP header")
        magic, version = struct.unpack_from("<4sI", data)
        if magic != VBSP_MAGIC:
            raise SourceBspError(f"{source_name}: expected VBSP magic, found {magic!r}")
        if version not in SUPPORTED_VERSIONS:
            raise SourceBspError(f"{source_name}: unsupported VBSP version {version}")
        lumps = [Lump(*struct.unpack_from("<III4s", data, 8 + index * 16)) for index in range(LUMP_COUNT)]

        def chunk(index: int) -> bytes:
            lump = lumps[index]
            if lump.offset < 0 or lump.length < 0 or lump.offset + lump.length > len(data):
                raise SourceBspError(f"{source_name}: lump {index} lies outside file")
            return data[lump.offset : lump.offset + lump.length]

        vertices = list(_records(chunk(L_VERTICES), "<3f", "vertices"))
        edges = list(_records(chunk(L_EDGES), "<2H", "edges"))
        surfedges = [item[0] for item in _records(chunk(L_SURFEDGES), "<i", "surfedges")]
        raw_texinfo = list(_records(chunk(L_TEXINFO), "<16f2i", "texinfo"))
        texinfo = [TexInfo(tuple(row[0:4]), tuple(row[4:8]), row[16], row[17]) for row in raw_texinfo]
        names = _texdata_names(chunk(L_TEXDATA_STRING_DATA), chunk(L_TEXDATA_STRING_TABLE))
        texdata = []
        for row in _records(chunk(L_TEXDATA), "<3f5i", "texdata"):
            name_index, width, height = row[3], row[5], row[6]
            texdata.append(TexData(names[name_index] if 0 <= name_index < len(names) else "missing", width, height))
        faces = []
        for row in _records(chunk(L_FACES), FACE_FORMAT, "faces"):
            faces.append(Face(row[0], row[3], row[4], row[5], row[6]))
        models = [
            BspModel(tuple(row[0:3]), tuple(row[3:6]), tuple(row[6:9]), row[9], row[10], row[11])
            for row in _records(chunk(L_MODELS), "<9f3i", "models")
        ]
        if not models:
            raise SourceBspError(f"{source_name}: BSP contains no world model")
        world_first_face, world_face_count = models[0].first_face, models[0].face_count
        if (
            world_first_face < 0
            or world_face_count < 0
            or world_first_face + world_face_count > len(faces)
        ):
            raise SourceBspError(f"{source_name}: invalid world-model face range")
        planes = [
            Plane(tuple(row[0:3]), row[3])
            for row in _records(chunk(L_PLANES), "<3ffi", "planes")
        ]
        brushes = [Brush(*row) for row in _records(chunk(L_BRUSHES), "<3i", "brushes")]
        brushsides = [
            BrushSide(row[0], row[1], bool(row[3]))
            for row in _records(chunk(L_BRUSHSIDES), "<Hhhh", "brush sides")
        ]
        nodes = list(_records(chunk(L_NODES), "<3i3h3hHHh2x", "nodes"))
        leaves = list(_records(chunk(L_LEAVES), "<ihh3h3h4Hh2x", "leaves"))
        leafbrushes = [row[0] for row in _records(chunk(L_LEAFBRUSHES), "<H", "leaf brushes")]
        model_brushes = [
            _model_brush_indices(model.headnode, nodes, leaves, leafbrushes, len(brushes), source_name)
            for model in models
        ]
        dispinfo = []
        raw_dispinfo = chunk(L_DISPINFO)
        if len(raw_dispinfo) % DISPINFO_SIZE:
            raise SourceBspError(f"{source_name}: malformed displacement info lump")
        for offset in range(0, len(raw_dispinfo), DISPINFO_SIZE):
            start = struct.unpack_from("<3f", raw_dispinfo, offset)
            first_vertex, _first_tri, power = struct.unpack_from("<3i", raw_dispinfo, offset + 12)
            if not 1 <= power <= 4:
                raise SourceBspError(f"{source_name}: invalid displacement power {power}")
            dispinfo.append(DispInfo(start, first_vertex, power))
        dispverts = [DispVertex(tuple(row[0:3]), row[3]) for row in _records(chunk(L_DISPVERTS), "<3f2f", "dispverts")]
        static_props = _parse_static_props(data, chunk(L_GAME_LUMP), source_name)
        return cls(
            source_name,
            version,
            _parse_entities(chunk(L_ENTITIES)),
            vertices,
            edges,
            surfedges,
            faces,
            texinfo,
            texdata,
            planes,
            brushes,
            brushsides,
            models,
            model_brushes,
            dispinfo,
            dispverts,
            static_props,
            chunk(L_PAKFILE),
        )

    def spawns(self) -> list[Spawn]:
        teams = {
            "info_player_counterterrorist": "alpha",
            "info_player_terrorist": "omega",
            "info_player_start": "neutral",
            "info_player_deathmatch": "neutral",
        }
        output = []
        for entity in self.entities:
            classname = entity.get("classname", "").lower()
            if classname not in teams or "origin" not in entity:
                continue
            origin = _float_tuple(entity["origin"], 3)
            angles = _float_tuple(entity.get("angles", "0 0 0"), 3)
            # Source yaw 0 faces +X. Eclipse yaw 0 faces +Y; reflecting X also
            # reverses yaw so the authored view remains aligned with the map.
            output.append(Spawn(classname, teams[classname], origin, (90.0 - angles[1]) % 360.0))
        return output

    def world_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        for entity in self.entities:
            if entity.get("classname", "").lower() != "worldspawn":
                continue
            if "world_mins" in entity and "world_maxs" in entity:
                return _float_tuple(entity["world_mins"], 3), _float_tuple(entity["world_maxs"], 3)
        return None

    def playable_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        bounds = self.world_bounds()
        if any(entity.get("classname", "").lower() == "sky_camera" for entity in self.entities):
            spawns = self.spawns()
            if spawns:
                margin = 4096.0
                bounds = (
                    tuple(min(spawn.origin[axis] for spawn in spawns) - margin for axis in range(3)),
                    tuple(max(spawn.origin[axis] for spawn in spawns) + margin for axis in range(3)),
                )
        return bounds

    def playable_triangles(self, displacement_lod: int = 2) -> list[Triangle]:
        """Return visible world surfaces, excluding tools and the 3D skybox."""
        triangles = self.triangles(displacement_lod)
        liquid_materials = {
            self.texdata[info.texdata].name
            for info in self.texinfo
            if info.flags & SURF_WARP and 0 <= info.texdata < len(self.texdata)
        }
        bounds = self.playable_bounds()
        output = []
        for triangle in triangles:
            if (
                triangle.material.lower().startswith(SOURCE_TOOL_PREFIX)
                or triangle.material in liquid_materials
            ):
                continue
            if bounds:
                minimum, maximum = bounds
                if any(
                    point[axis] < minimum[axis] - 1.0 or point[axis] > maximum[axis] + 1.0
                    for point in triangle.points
                    for axis in range(3)
                ):
                    continue
            output.append(triangle)
        return output

    def playable_collision_triangles(self, displacement_lod: int = 2) -> list[Triangle]:
        """Reconstruct authored solid/player-clip brushes plus displacement terrain."""
        bounds = self.playable_bounds()
        model = self.models[0]
        displacement_faces = [
            face
            for face in self.faces[model.first_face : model.first_face + model.face_count]
            if face.dispinfo >= 0
        ]
        displacement_planes = {face.plane for face in displacement_faces}
        output = []
        for brush_index in self.model_brushes[0]:
            brush = self.brushes[brush_index]
            if not brush.contents & (CONTENTS_SOLID | CONTENTS_PLAYERCLIP):
                continue
            for points in _brush_triangles(
                brush, self.brushsides, self.planes, displacement_planes
            ):
                center = tuple(sum(point[axis] for point in points) / 3.0 for axis in range(3))
                if bounds and any(
                    center[axis] < bounds[0][axis] - 1.0
                    or center[axis] > bounds[1][axis] + 1.0
                    for axis in range(3)
                ):
                    continue
                output.append(Triangle("collision/brush", points, ((0.0, 0.0),) * 3))

        # Displacements replace their source brush face with deformed terrain;
        # retain those triangles so slopes follow the compiled map exactly.
        for triangle in self._triangles_for_faces(displacement_faces, displacement_lod):
            if not bounds or all(
                bounds[0][axis] - 1.0 <= point[axis] <= bounds[1][axis] + 1.0
                for point in triangle.points
                for axis in range(3)
            ):
                output.append(triangle)
        return output

    def playable_water_volumes(self) -> list[MaterialVolume]:
        """Return authored world-water brush bounds, excluding the 3D skybox."""
        bounds = self.playable_bounds()
        output = []
        for brush_index in self.model_brushes[0]:
            brush = self.brushes[brush_index]
            if not brush.contents & CONTENTS_WATER:
                continue
            triangles = _brush_triangles(brush, self.brushsides, self.planes)
            points = [point for triangle in triangles for point in triangle]
            if not points:
                continue
            axes = list(zip(*points))
            minimum = tuple(min(axis) for axis in axes)
            maximum = tuple(max(axis) for axis in axes)
            center = tuple((minimum[axis] + maximum[axis]) / 2.0 for axis in range(3))
            if bounds and any(
                center[axis] < bounds[0][axis] - 1.0
                or center[axis] > bounds[1][axis] + 1.0
                for axis in range(3)
            ):
                continue
            output.append(MaterialVolume(minimum, maximum))
        return output

    def neutral_backing_triangles(self) -> list[Triangle]:
        """Return a slightly inset visual shell behind authored solid brushes."""
        bounds = self.playable_bounds()
        model = self.models[0]
        displacement_planes = {
            face.plane
            for face in self.faces[model.first_face : model.first_face + model.face_count]
            if face.dispinfo >= 0
        }
        output = []
        for brush_index in self.model_brushes[0]:
            brush = self.brushes[brush_index]
            solid = bool(brush.contents & CONTENTS_SOLID)
            player_clip = bool(brush.contents & CONTENTS_PLAYERCLIP)
            if not (solid or player_clip):
                continue
            for points in _brush_triangles(
                brush, self.brushsides, self.planes, displacement_planes
            ):
                center = tuple(sum(point[axis] for point in points) / 3.0 for axis in range(3))
                if bounds and any(
                    center[axis] < bounds[0][axis] - 1.0
                    or center[axis] > bounds[1][axis] + 1.0
                    for axis in range(3)
                ):
                    continue
                normal = _normalize(_cross(_sub(points[1], points[0]), _sub(points[2], points[0])))
                # Player clips are useful fallback only for wall-like barriers.
                # Keeping their horizontal caps invisible avoids painting over
                # valid floors, ramps and anti-exploit volumes above the map.
                if player_clip and not solid and abs(normal[2]) >= 0.25:
                    continue
                inset = tuple(
                    tuple(point[axis] - normal[axis] * 2.0 for axis in range(3))
                    for point in points
                )
                output.append(Triangle("fallback/neutral", inset, ((0.0, 0.0),) * 3))
        return output

    def _face_polygon(self, face: Face) -> list[tuple[float, float, float]]:
        if face.first_edge < 0 or face.edge_count < 3 or face.first_edge + face.edge_count > len(self.surfedges):
            return []
        points = []
        for surfedge in self.surfedges[face.first_edge : face.first_edge + face.edge_count]:
            edge_index = abs(surfedge)
            if edge_index >= len(self.edges):
                return []
            edge = self.edges[edge_index]
            vertex_index = edge[0] if surfedge >= 0 else edge[1]
            if vertex_index >= len(self.vertices):
                return []
            points.append(self.vertices[vertex_index])
        return points

    def triangles(self, displacement_lod: int = 2) -> list[Triangle]:
        if displacement_lod < 0:
            raise SourceBspError("displacement LOD reduction cannot be negative")
        model = self.models[0]
        return self._triangles_for_faces(
            self.faces[model.first_face : model.first_face + model.face_count], displacement_lod
        )

    def _triangles_for_faces(self, faces: Iterable[Face], displacement_lod: int) -> list[Triangle]:
        output = []
        for face in faces:
            if not 0 <= face.texinfo < len(self.texinfo):
                continue
            info = self.texinfo[face.texinfo]
            if not 0 <= info.texdata < len(self.texdata):
                continue
            texture = self.texdata[info.texdata]
            polygon = self._face_polygon(face)
            if len(polygon) < 3:
                continue
            if 0 <= face.dispinfo < len(self.dispinfo) and len(polygon) == 4:
                points = self._displacement_points(polygon, self.dispinfo[face.dispinfo], displacement_lod)
                for row in range(len(points) - 1):
                    for column in range(len(points[row]) - 1):
                        a, b = points[row][column], points[row][column + 1]
                        c, d = points[row + 1][column + 1], points[row + 1][column]
                        for tri in ((a, b, c), (a, c, d)):
                            output.append(Triangle(texture.name, tri, tuple(_uv(point, info, texture) for point in tri)))
            else:
                for index in range(1, len(polygon) - 1):
                    tri = (polygon[0], polygon[index], polygon[index + 1])
                    output.append(Triangle(texture.name, tri, tuple(_uv(point, info, texture) for point in tri)))
        return output

    def _displacement_points(
        self, polygon: list[tuple[float, float, float]], displacement: DispInfo, lod: int
    ) -> list[list[tuple[float, float, float]]]:
        start_index = min(range(4), key=lambda index: _distance(polygon[index], displacement.start))
        corners = polygon[start_index:] + polygon[:start_index]
        side = 1 << displacement.power
        count = (side + 1) ** 2
        if displacement.first_vertex < 0 or displacement.first_vertex + count > len(self.dispverts):
            raise SourceBspError(f"{self.source_name}: displacement vertices lie outside lump")
        step = min(1 << lod, side)
        sampled = list(range(0, side + 1, step))
        if sampled[-1] != side:
            sampled.append(side)
        output = []
        for y in sampled:
            row = []
            ty = y / side
            for x in sampled:
                tx = x / side
                # Source treats the first face edge as the outer grid axis:
                # row interpolation walks point 0 -> 1 and point 3 -> 2,
                # then the inner coordinate interpolates between those edges.
                base = tuple(
                    corners[0][axis] * (1 - tx) * (1 - ty)
                    + corners[1][axis] * (1 - tx) * ty
                    + corners[2][axis] * tx * ty
                    + corners[3][axis] * tx * (1 - ty)
                    for axis in range(3)
                )
                displaced = self.dispverts[displacement.first_vertex + y * (side + 1) + x]
                row.append(tuple(base[axis] + displaced.direction[axis] * displaced.distance for axis in range(3)))
            output.append(row)
        return output

    def bounds(self, triangles: Iterable[Triangle]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        points = [point for triangle in triangles for point in triangle.points]
        if not points:
            raise SourceBspError(f"{self.source_name}: no renderable surfaces")
        axes = list(zip(*points))
        return tuple(min(axis) for axis in axes), tuple(max(axis) for axis in axes)

    def manifest(self, triangles: list[Triangle], displacement_lod: int) -> dict[str, object]:
        minimum, maximum = self.bounds(triangles)
        return {
            "format": "VBSP",
            "version": self.version,
            "source": self.source_name,
            "bounds": {"minimum": minimum, "maximum": maximum},
            "displacement_lod": displacement_lod,
            "counts": {
                "entities": len(self.entities),
                "spawns": len(self.spawns()),
                "vertices": len(self.vertices),
                "faces": self.models[0].face_count,
                "models": len(self.models),
                "world_brushes": len(self.model_brushes[0]),
                "displacements": len(self.dispinfo),
                "static_props": len(self.static_props),
                "static_prop_models": len({prop.model for prop in self.static_props}),
                "water_brushes": len(self.playable_water_volumes()),
                "triangles": len(triangles),
                "materials": len({triangle.material for triangle in triangles}),
            },
            "spawns": [spawn.__dict__ for spawn in self.spawns()],
        }


def write_eclipse_stage(
    source: Path,
    bsp: SourceBsp,
    stage: Path,
    vpk_path: Path | None = None,
    scale: float = 0.25,
    displacement_lod: int = 2,
    prop_manifest: dict[str, object] | None = None,
    include_neutral_backing: bool = False,
) -> dict[str, object]:
    if scale <= 0:
        raise SourceBspError("Eclipse scale must be positive")
    triangles = bsp.playable_triangles(displacement_lod)
    collision_source = bsp.playable_collision_triangles(displacement_lod)
    neutral_backing = bsp.neutral_backing_triangles() if include_neutral_backing else []
    water_volumes = bsp.playable_water_volumes()
    # Eclipse combines all OBJ meshes into one ushort-indexed VBO.  Chunking
    # protects each BIH mesh, but it cannot prevent a model-wide index wrap.
    # This exporter deliberately duplicates render vertices, so the bound is
    # exact and provides a clear request to increase displacement LOD.
    render_vertex_count = len(triangles) * 3
    if render_vertex_count > OBJ_INDEX_LIMIT:
        raise SourceBspError(
            f"simplified render mesh needs {render_vertex_count} OBJ vertices; "
            f"Eclipse supports at most {OBJ_INDEX_LIMIT}, increase --displacement-lod"
        )
    stem = source.stem
    model_rel = Path("csgopen") / "imported" / stem
    model_dir = stage / "data" / model_rel
    maps_dir = stage / "data" / "maps"
    profile_dir = stage / "profile"
    for directory in (model_dir, maps_dir, profile_dir):
        directory.mkdir(parents=True, exist_ok=True)

    mesh_bindings: dict[str, str] = {}
    with (model_dir / f"{stem}.obj").open("wb") as output:
        render_stats, material_meshes = _write_render_obj(output, triangles, bsp.source_name)

    vpk = VpkArchive(vpk_path) if vpk_path else None
    content = ContentStore(bsp.pakfile, vpk)
    texture_stats = _extract_materials(content, material_meshes, model_dir, mesh_bindings)

    model_config = [
        f'objload "{stem}.obj"',
        "mdlcullface 0",
        f"mdlscale {scale * 100:.9g}",
        "mdlcollide 0",
    ]
    for mesh, texture in sorted(mesh_bindings.items()):
        model_config.insert(1, f'objskin "{mesh}" "{texture}"')
    (model_dir / "obj.cfg").write_text("\n".join(model_config) + "\n", encoding="utf-8")
    fallback_rel = None
    fallback_stats = {"vertices": 0, "triangles": 0, "meshes": 0, "materials": 0}
    if include_neutral_backing:
        fallback_rel = model_rel / "fallback"
        fallback_dir = stage / "data" / fallback_rel
        fallback_dir.mkdir(parents=True, exist_ok=True)
        with (fallback_dir / "fallback.obj").open("wb") as output:
            fallback_stats, _fallback_materials = _write_render_obj(
                output, neutral_backing, f"{bsp.source_name} neutral backing"
            )
        if fallback_stats["vertices"] > OBJ_INDEX_LIMIT:
            raise SourceBspError(
                f"neutral backing needs {fallback_stats['vertices']} OBJ vertices; "
                f"Eclipse supports at most {OBJ_INDEX_LIMIT}"
            )
        (fallback_dir / "neutral.dds").write_bytes(_solid_dxt1_dds(0x8410))
        (fallback_dir / "obj.cfg").write_text(
            "\n".join(
                (
                    'objload "fallback.obj"',
                    'objskin "*" "neutral.dds"',
                    "mdlcullface 0",
                    f"mdlscale {scale * 100:.9g}",
                    "mdlcollide 0",
                )
            )
            + "\n",
            encoding="utf-8",
        )
    collision_models = []
    collision_triangles = collision_vertices = 0
    for tile, tile_triangles in sorted(_collision_partitions(collision_source).items()):
        suffix = "_".join(str(value).replace("-", "n") for value in tile)
        collision_rel = model_rel / "collision" / f"tile_{suffix}"
        collision_dir = stage / "data" / collision_rel
        collision_dir.mkdir(parents=True, exist_ok=True)
        with (collision_dir / "collision.obj").open("wb") as output:
            stats = _write_collision_obj(output, tile_triangles, bsp.source_name)
        collision_triangles += stats["triangles"]
        collision_vertices += stats["vertices"]
        (collision_dir / "obj.cfg").write_text(
            "\n".join(
                (
                    'objload "collision.obj"',
                    'objblend "*" 0',
                    "mdlcullface 0",
                    f"mdlscale {scale * 100:.9g}",
                    "mdltricollide 1",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        collision_models.append(collision_rel)
    collision_stats = {
        "vertices": collision_vertices,
        "triangles": collision_triangles,
        "models": len(collision_models),
        "tile_size": COLLISION_TILE_SIZE,
    }
    map_config = [
        f"// Generated directly from {bsp.source_name}",
        "setenv ambient 0x808080",
        "setenv skylight 0xFFFFFF",
        f'mapmodel "{model_rel.as_posix()}"',
    ]
    map_config.extend(f'mapmodel "{path.as_posix()}"' for path in collision_models)
    prop_models = [str(path) for path in (prop_manifest or {}).get("models", [])]
    map_config.extend(f'mapmodel "{path}"' for path in prop_models)
    if fallback_rel is not None:
        map_config.append(f'mapmodel "{fallback_rel.as_posix()}"')
    (maps_dir / f"{stem}.cfg").write_text("\n".join(map_config) + "\n", encoding="utf-8")

    minimum, maximum = bsp.bounds(triangles)
    target_minimum = _target_position((maximum[0], minimum[1], minimum[2]))
    target_maximum = _target_position((minimum[0], maximum[1], maximum[2]))
    extents = [(maximum[index] - minimum[index]) * scale for index in range(3)]
    margin = 64.0
    required = max(value + 2 * margin for value in extents)
    world_scale = max(10, min(16, math.ceil(math.log2(max(required, 1.0)))))
    world_size = 1 << world_scale
    offset = (
        margin - target_minimum[0] * scale,
        margin - target_minimum[1] * scale,
        world_size / 2 + margin - target_minimum[2] * scale,
    )
    commands = [
        "sourceimport_done = 0",
        "sourceimport_finish = [",
        "    if (= $sourceimport_done 0) [",
        "        sourceimport_done = 1",
        "        edittoggle",
        "        newmapfloor 0",
        f"        newmap {world_scale} {stem}",
        "        mapmodelreset 0",
        f'        exec "maps/{stem}.cfg"',
    ]
    water_selections = [
        _material_selection(volume, scale, offset, 8) for volume in water_volumes
    ]
    for origin, size, grid in water_selections:
        commands.append(
            "        editmatbox water %d %d %d %d %d %d %d"
            % (*origin, *size, grid)
        )
    _append_positioned_entity(commands, "newent mapmodel 0 0 0 0 100 100", offset)
    for model_index in range(1, len(collision_models) + 1):
        _append_positioned_entity(
            commands, f"newent mapmodel {model_index} 0 0 0 100 100", offset
        )
    prop_model_start = 1 + len(collision_models)
    for model_index in range(prop_model_start, prop_model_start + len(prop_models)):
        _append_positioned_entity(
            commands, f"newent mapmodel {model_index} 0 0 0 100 100", offset
        )
    if fallback_rel is not None:
        fallback_model_index = prop_model_start + len(prop_models)
        _append_positioned_entity(
            commands, f"newent mapmodel {fallback_model_index} 0 0 0 100 100", offset
        )
    spawn_entries = []
    surface_points = [triangle.points for triangle in collision_source]
    for spawn in bsp.spawns():
        floor_z = _support_floor(spawn.origin, surface_points)
        spawn_entries.append((spawn, floor_z))
    for spawn_id, (spawn, floor_z) in enumerate(spawn_entries):
        team = {"neutral": 0, "alpha": 1, "omega": 2}[spawn.team]
        target = _target_position(spawn.origin)
        position = (
            target[0] * scale + offset[0],
            target[1] * scale + offset[1],
            (floor_z if floor_z is not None else spawn.origin[2]) * scale
            + offset[2]
            + SPAWN_CLEARANCE,
        )
        _append_positioned_entity(
            commands,
            f"newent playerstart {team} {round(spawn.yaw) % 360} 0 0 0 {spawn_id} 0",
            position,
        )
    commands.extend(
        (
            f'        maptitle "Imported {stem}"',
            '        mapauthor "Converted directly from a Source BSP"',
            f'        savemap "{stem}"',
            f'        echo "SOURCEIMPORT_DONE {stem}"',
            "        sleep 1000 [quit]",
            "    ]",
            "]",
            f'edit "{stem}"',
            "sleep 30000 [sourceimport_finish]",
        )
    )
    (profile_dir / "build-map.cfg").write_text("\n".join(commands) + "\n", encoding="utf-8")
    (profile_dir / "play.cfg").write_text(
        f'exec "config/csgopen/tdm.cfg"\nexec "config/csgopen/client.cfg"\ntdm {stem}\n', encoding="utf-8"
    )
    conversion = {
        "map": stem,
        "model": model_rel.as_posix(),
        "scale": scale,
        "world_scale": world_scale,
        "world_size": world_size,
        "offset": offset,
        "render_mesh": render_stats,
        "collision_mesh": collision_stats,
        "textures": texture_stats,
        "water": {
            "brushes": len(water_volumes),
            "selections": len(water_selections),
            "grid": 8,
        },
        "neutral_backing": {"enabled": include_neutral_backing, **fallback_stats},
        "static_props": (prop_manifest or {}).get("counts"),
        "spawns_written": len(spawn_entries),
        "spawns_with_support": sum(floor_z is not None for _spawn, floor_z in spawn_entries),
        "expected_mpz": str(profile_dir / "maps" / f"{stem}.mpz"),
    }
    (stage / f"{stem}.json").write_text(
        json.dumps({**bsp.manifest(triangles, displacement_lod), "eclipse": conversion}, indent=2) + "\n",
        encoding="utf-8",
    )
    return conversion


def _material_selection(
    volume: MaterialVolume,
    scale: float,
    offset: tuple[float, float, float],
    grid: int,
) -> tuple[tuple[int, int, int], tuple[int, int, int], int]:
    """Convert reflected Source bounds to one grid-aligned Eclipse selection."""
    if grid <= 0 or grid & (grid - 1):
        raise SourceBspError("material grid must be a positive power of two")
    target_minimum = _target_position(
        (volume.maximum[0], volume.minimum[1], volume.minimum[2])
    )
    target_maximum = _target_position(
        (volume.minimum[0], volume.maximum[1], volume.maximum[2])
    )
    world_minimum = tuple(
        target_minimum[axis] * scale + offset[axis] for axis in range(3)
    )
    world_maximum = tuple(
        target_maximum[axis] * scale + offset[axis] for axis in range(3)
    )
    origin = tuple(math.floor(value / grid) * grid for value in world_minimum)
    end = tuple(math.ceil(value / grid) * grid for value in world_maximum)
    size = tuple(max(1, (end[axis] - origin[axis]) // grid) for axis in range(3))
    return origin, size, grid


def _append_positioned_entity(
    commands: list[str], command: str, position: tuple[float, float, float]
) -> None:
    """Create one entity without leaking its editor selection to the next."""
    commands.extend(
        (
            f"        {command}",
            "        entpos %.9g %.9g %.9g" % position,
            "        entcancel",
        )
    )


def _write_render_obj(
    output: BinaryIO, triangles: list[Triangle], source_name: str
) -> tuple[dict[str, int], dict[str, list[str]]]:
    output.write(f"# Source BSP render mesh from {source_name}\n".encode())
    by_material: dict[str, list[Triangle]] = defaultdict(list)
    for triangle in triangles:
        by_material[triangle.material].append(triangle)
    material_meshes: dict[str, list[str]] = {}
    vertex = 1
    mesh_count = 0
    for material, items in sorted(by_material.items()):
        names = []
        chunks = _safe_chunks(items, TRIANGLES_PER_MESH)
        for chunk_index, chunk in enumerate(chunks):
            mesh = f"{_material_name(material)}__{chunk_index}"
            names.append(mesh)
            mesh_count += 1
            output.write(f"g {mesh}\n".encode())
            for triangle in chunk:
                for point in triangle.points:
                    output.write(("v %.9g %.9g %.9g\n" % _obj_position(point)).encode())
                for uv in triangle.uvs:
                    output.write(("vt %.9g %.9g\n" % uv).encode())
                output.write(
                    f"f {vertex}/{vertex} {vertex + 1}/{vertex + 1} {vertex + 2}/{vertex + 2}\n".encode()
                )
                vertex += 3
        material_meshes[material] = names
    return {
        "vertices": vertex - 1,
        "triangles": len(triangles),
        "meshes": mesh_count,
        "materials": len(by_material),
    }, material_meshes


def _write_collision_obj(output: BinaryIO, triangles: list[Triangle], source_name: str) -> dict[str, int]:
    output.write(f"# Source BSP collision mesh from {source_name}\n".encode())
    _vertex, stats = _write_collision_groups(output, triangles, 1)
    return stats


def _collision_partitions(
    triangles: list[Triangle], tile_size: float = COLLISION_TILE_SIZE
) -> dict[tuple[int, int], list[Triangle]]:
    """Group triangles by horizontal centroid for local mapmodel collision."""
    partitions: dict[tuple[int, int], list[Triangle]] = defaultdict(list)
    for triangle in triangles:
        center_x = sum(point[0] for point in triangle.points) / 3.0
        center_y = sum(point[1] for point in triangle.points) / 3.0
        partitions[(math.floor(center_x / tile_size), math.floor(center_y / tile_size))].append(
            triangle
        )
    return partitions


def _write_collision_groups(
    output: BinaryIO, triangles: list[Triangle], vertex: int
) -> tuple[int, dict[str, int]]:
    # The groups are marked collision-only with objtricollide.  They therefore
    # never enter Eclipse's ushort-indexed render VBO, while the BIH can retain
    # both windings and collide reliably from either side.
    collision = [
        points
        for triangle in triangles
        for points in (triangle.points, tuple(reversed(triangle.points)))
    ]
    first_vertex = vertex
    for group, chunk in enumerate(_safe_chunks(collision, COLLISION_TRIANGLES_PER_MESH)):
        output.write(f"g collision_{group}\n".encode())
        for triangle in chunk:
            for point in triangle:
                output.write(("v %.9g %.9g %.9g\n" % _obj_position(point)).encode())
            output.write(f"f {vertex} {vertex + 1} {vertex + 2}\n".encode())
            vertex += 3
    return vertex, {"vertices": vertex - first_vertex, "triangles": len(collision)}


def _extract_materials(
    content: ContentStore,
    material_meshes: dict[str, list[str]],
    model_dir: Path,
    bindings: dict[str, str],
) -> dict[str, int]:
    resolved = 0
    missing = 0
    unsupported = 0
    cache: dict[str, str | None] = {}
    for material, meshes in material_meshes.items():
        base = _resolve_base_texture(content, material, cache, set())
        if not base:
            missing += 1
            continue
        data = content.read(f"materials/{base}.vtf")
        if data is None:
            missing += 1
            continue
        filename = _material_name(material) + ".dds"
        try:
            (model_dir / filename).write_bytes(_vtf_to_dds(data))
        except SourceBspError:
            unsupported += 1
            continue
        for mesh in meshes:
            bindings[mesh] = filename
        resolved += 1
    return {"resolved": resolved, "missing": missing, "unsupported_format": unsupported}


def _resolve_base_texture(
    content: ContentStore, material: str, cache: dict[str, str | None], active: set[str]
) -> str | None:
    normalized = material.lower().removeprefix("materials/").removesuffix(".vmt")
    if normalized in cache:
        return cache[normalized]
    if normalized in active:
        return None
    active.add(normalized)
    data = content.read(f"materials/{normalized}.vmt")
    if data is None:
        result = None
    else:
        text = re.sub(r"//[^\r\n]*", "", data.decode("utf-8", "replace"))
        match = re.search(r'"?\$basetexture"?\s+"([^\"]+)"', text, re.I)
        if match:
            result = match.group(1).replace("\\", "/").lower().removeprefix("materials/").removesuffix(".vtf")
        else:
            include = re.search(r'"?include"?\s+"([^\"]+)"', text, re.I)
            result = _resolve_base_texture(content, include.group(1), cache, active) if include else None
    active.remove(normalized)
    cache[normalized] = result
    return result


def _vtf_to_dds(data: bytes) -> bytes:
    if len(data) < 64 or data[:4] != b"VTF\0":
        raise SourceBspError("invalid VTF header")
    major, minor = struct.unpack_from("<II", data, 4)
    if major != 7:
        raise SourceBspError(f"unsupported VTF version {major}.{minor}")
    header_size = struct.unpack_from("<I", data, 12)[0]
    width, height = struct.unpack_from("<HH", data, 16)
    flags = struct.unpack_from("<I", data, 20)[0]
    frames = struct.unpack_from("<H", data, 24)[0]
    high_format = struct.unpack_from("<i", data, 52)[0]
    mip_count = data[56]
    low_format = struct.unpack_from("<i", data, 57)[0]
    low_width, low_height = data[61], data[62]
    if high_format not in (13, 14, 15):
        raise SourceBspError(f"unsupported VTF image format {high_format}")
    if not width or not height or not mip_count:
        raise SourceBspError("invalid VTF dimensions")
    high_offset = None
    if minor >= 3 and len(data) >= 80:
        resource_count = struct.unpack_from("<I", data, 68)[0]
        for index in range(resource_count):
            position = 80 + index * 8
            if position + 8 > len(data):
                break
            tag = data[position : position + 3]
            resource_offset = struct.unpack_from("<I", data, position + 4)[0]
            if tag == b"0\0\0":
                high_offset = resource_offset
                break
    if high_offset is None:
        low_size = _vtf_image_size(low_width, low_height, low_format) if low_width and low_height else 0
        high_offset = header_size + low_size
    faces = 6 if flags & 0x4000 else 1
    frames = max(frames, 1)
    offset = high_offset
    for mip in range(mip_count - 1, 0, -1):
        mip_width = max(1, width >> mip)
        mip_height = max(1, height >> mip)
        offset += _vtf_image_size(mip_width, mip_height, high_format) * frames * faces
    image_size = _vtf_image_size(width, height, high_format)
    image = data[offset : offset + image_size]
    if len(image) != image_size:
        raise SourceBspError("truncated VTF high-resolution image")
    fourcc = {13: b"DXT1", 14: b"DXT3", 15: b"DXT5"}[high_format]
    dds_flags = 0x00081007
    pixel_format = struct.pack("<II4s5I", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    header = struct.pack("<7I11I", 124, dds_flags, height, width, image_size, 0, 1, *([0] * 11))
    caps = struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + header + pixel_format + caps + image


def _solid_dxt1_dds(rgb565: int) -> bytes:
    """Create a 4x4 opaque single-colour DXT1 texture."""
    image = struct.pack("<HHI", rgb565, max(0, rgb565 - 1), 0)
    pixel_format = struct.pack("<II4s5I", 32, 0x4, b"DXT1", 0, 0, 0, 0, 0)
    header = struct.pack("<7I11I", 124, 0x00081007, 4, 4, len(image), 0, 1, *([0] * 11))
    caps = struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + header + pixel_format + caps + image


def _vtf_image_size(width: int, height: int, image_format: int) -> int:
    if image_format == 13:
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    if image_format in (14, 15):
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    raw_bpp = {0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 1, 6: 2, 8: 1, 11: 4, 12: 4, 16: 4}
    if image_format in raw_bpp:
        return width * height * raw_bpp[image_format]
    raise SourceBspError(f"unsupported VTF image format {image_format}")


def _safe_chunks(items: Sequence, size: int) -> list[Sequence]:
    chunks = [items[index : index + size] for index in range(0, len(items), size)]
    # Eclipse's recursive BIH builder cannot split a one-triangle mesh safely.
    # Duplicate an isolated render triangle; coincident geometry is visually
    # identical and gives the builder a valid two-item base case.
    if len(chunks) == 1 and len(chunks[0]) == 1:
        chunks[0] = list(chunks[0]) * 2
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-1] = list(chunks[-2][-1:]) + list(chunks[-1])
        chunks[-2] = chunks[-2][:-1]
    return chunks


def _uv(point: tuple[float, float, float], info: TexInfo, texture: TexData) -> tuple[float, float]:
    width = max(texture.width, 1)
    height = max(texture.height, 1)
    u = (sum(point[index] * info.uaxis[index] for index in range(3)) + info.uaxis[3]) / width
    v = (sum(point[index] * info.vaxis[index] for index in range(3)) + info.vaxis[3]) / height
    return u, 1.0 - v


def _texdata_names(data: bytes, table: bytes) -> list[str]:
    names = []
    for (offset,) in _records(table, "<I", "texture string table"):
        if offset >= len(data):
            names.append("missing")
            continue
        end = data.find(b"\0", offset)
        names.append(data[offset : end if end >= 0 else len(data)].decode("utf-8", "replace"))
    return names


def _read_cstring(stream: BinaryIO) -> str:
    value = bytearray()
    while True:
        char = stream.read(1)
        if not char:
            raise SourceBspError("truncated null-terminated string")
        if char == b"\0":
            return value.decode("utf-8", "replace")
        value.extend(char)


def _model_brush_indices(
    headnode: int,
    nodes: Sequence[tuple],
    leaves: Sequence[tuple],
    leafbrushes: Sequence[int],
    brush_count: int,
    source_name: str,
) -> tuple[int, ...]:
    pending = [headnode]
    seen_nodes = set()
    seen_leaves = set()
    while pending:
        node = pending.pop()
        if node >= 0:
            if node in seen_nodes:
                continue
            if node >= len(nodes):
                raise SourceBspError(f"{source_name}: model references invalid BSP node {node}")
            seen_nodes.add(node)
            pending.extend(nodes[node][1:3])
        else:
            leaf = -1 - node
            if leaf >= len(leaves):
                raise SourceBspError(f"{source_name}: model references invalid BSP leaf {leaf}")
            seen_leaves.add(leaf)
    result = set()
    for leaf_index in seen_leaves:
        leaf = leaves[leaf_index]
        first, count = leaf[11], leaf[12]
        if first + count > len(leafbrushes):
            raise SourceBspError(f"{source_name}: leaf brush range lies outside lump")
        result.update(leafbrushes[first : first + count])
    if any(index >= brush_count for index in result):
        raise SourceBspError(f"{source_name}: invalid brush index in model tree")
    return tuple(sorted(result))


def _brush_triangles(
    brush: Brush,
    brushsides: Sequence[BrushSide],
    planes: Sequence[Plane],
    skip_planes: set[int] | frozenset[int] = frozenset(),
) -> list[tuple[tuple[float, float, float], ...]]:
    if brush.side_count < 4 or brush.first_side < 0:
        return []
    selected = brushsides[brush.first_side : brush.first_side + brush.side_count]
    if len(selected) != brush.side_count or any(side.plane >= len(planes) for side in selected):
        return []
    hull_planes = [planes[side.plane] for side in selected]
    vertices = []
    for first, second, third in itertools.combinations(hull_planes, 3):
        point = _plane_intersection(first, second, third)
        if point is None:
            continue
        if all(_dot(plane.normal, point) <= plane.distance + 0.05 for plane in hull_planes):
            if not any(_distance(point, old) < 0.05 for old in vertices):
                vertices.append(point)
    if len(vertices) < 4:
        return []

    output = []
    for side, plane in zip(selected, hull_planes):
        if side.bevel or side.plane in skip_planes:
            continue
        face = [
            point
            for point in vertices
            if abs(_dot(plane.normal, point) - plane.distance) < 0.1
        ]
        if len(face) < 3:
            continue
        center = tuple(sum(point[axis] for point in face) / len(face) for axis in range(3))
        helper = (0.0, 0.0, 1.0) if abs(plane.normal[2]) < 0.9 else (0.0, 1.0, 0.0)
        u = _normalize(_cross(helper, plane.normal))
        v = _cross(plane.normal, u)
        face.sort(
            key=lambda point: math.atan2(
                _dot(_sub(point, center), v), _dot(_sub(point, center), u)
            )
        )
        if _dot(_cross(_sub(face[1], face[0]), _sub(face[2], face[0])), plane.normal) < 0:
            face.reverse()
        for index in range(1, len(face) - 1):
            triangle = (face[0], face[index], face[index + 1])
            cross = _cross(_sub(triangle[1], triangle[0]), _sub(triangle[2], triangle[0]))
            if _dot(cross, cross) > 1e-8:
                output.append(triangle)
    return output


def _plane_intersection(first: Plane, second: Plane, third: Plane):
    second_cross_third = _cross(second.normal, third.normal)
    determinant = _dot(first.normal, second_cross_third)
    if abs(determinant) < 1e-8:
        return None
    third_cross_first = _cross(third.normal, first.normal)
    first_cross_second = _cross(first.normal, second.normal)
    return tuple(
        (
            first.distance * second_cross_third[axis]
            + second.distance * third_cross_first[axis]
            + third.distance * first_cross_second[axis]
        )
        / determinant
        for axis in range(3)
    )


def _records(data: bytes, fmt: str, label: str) -> Iterable[tuple]:
    size = struct.calcsize(fmt)
    if len(data) % size:
        raise SourceBspError(f"malformed {label} lump ({len(data)} bytes is not a multiple of {size})")
    return struct.iter_unpack(fmt, data)


def _parse_static_props(data: bytes, game_lump: bytes, source_name: str) -> list[StaticProp]:
    """Read CS:GO's version-10/11 ``sprp``/``prps`` game lump."""
    if not game_lump:
        return []
    if len(game_lump) < 4:
        raise SourceBspError(f"{source_name}: truncated game-lump header")
    count = struct.unpack_from("<i", game_lump)[0]
    if count < 0 or 4 + count * 16 > len(game_lump):
        raise SourceBspError(f"{source_name}: malformed game-lump directory")
    prop_lump = None
    version = 0
    for index in range(count):
        identifier, flags, item_version, offset, length = struct.unpack_from(
            "<IHHii", game_lump, 4 + index * 16
        )
        if struct.pack("<I", identifier) not in (b"prps", b"sprp"):
            continue
        if flags:
            raise SourceBspError(f"{source_name}: compressed static-prop lumps are unsupported")
        if offset < 0 or length < 0 or offset + length > len(data):
            raise SourceBspError(f"{source_name}: static-prop lump lies outside file")
        prop_lump = data[offset : offset + length]
        version = item_version
        break
    if prop_lump is None:
        return []
    if version not in (10, 11):
        raise SourceBspError(f"{source_name}: unsupported static-prop version {version}")

    cursor = 0

    def take_count(label: str) -> int:
        nonlocal cursor
        if cursor + 4 > len(prop_lump):
            raise SourceBspError(f"{source_name}: truncated static-prop {label}")
        value = struct.unpack_from("<i", prop_lump, cursor)[0]
        cursor += 4
        if value < 0:
            raise SourceBspError(f"{source_name}: negative static-prop {label}")
        return value

    model_count = take_count("model count")
    if cursor + model_count * 128 > len(prop_lump):
        raise SourceBspError(f"{source_name}: truncated static-prop model dictionary")
    models = []
    for index in range(model_count):
        raw = prop_lump[cursor + index * 128 : cursor + (index + 1) * 128]
        models.append(raw.split(b"\0", 1)[0].decode("utf-8", "replace").replace("\\", "/"))
    cursor += model_count * 128

    leaf_count = take_count("leaf count")
    if cursor + leaf_count * 2 > len(prop_lump):
        raise SourceBspError(f"{source_name}: truncated static-prop leaf list")
    cursor += leaf_count * 2
    prop_count = take_count("prop count")
    record_size = 76 if version == 10 else 80
    if cursor + prop_count * record_size != len(prop_lump):
        raise SourceBspError(f"{source_name}: malformed version-{version} static-prop records")

    output = []
    for index in range(prop_count):
        offset = cursor + index * record_size
        origin = struct.unpack_from("<3f", prop_lump, offset)
        angles = struct.unpack_from("<3f", prop_lump, offset + 12)
        model_index, _first_leaf, _leaf_count, solid, flags = struct.unpack_from(
            "<3H2B", prop_lump, offset + 24
        )
        skin = struct.unpack_from("<i", prop_lump, offset + 32)[0]
        scale = struct.unpack_from("<f", prop_lump, offset + 76)[0] if version == 11 else 1.0
        if not 0 <= model_index < len(models):
            raise SourceBspError(f"{source_name}: static prop references missing model {model_index}")
        output.append(StaticProp(models[model_index], origin, angles, solid, flags, skin, scale))
    return output


def _parse_entities(data: bytes) -> list[dict[str, str]]:
    text = data.rstrip(b"\0").decode("utf-8", "replace")
    result = []
    for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL):
        pairs = re.findall(r'"((?:\\.|[^"\\])*)"\s*"((?:\\.|[^"\\])*)"', body)
        result.append({key.replace(r'\"', '"'): value.replace(r'\"', '"') for key, value in pairs})
    return result


def _float_tuple(value: str, count: int) -> tuple[float, ...]:
    parts = value.split()
    if len(parts) != count:
        raise SourceBspError(f"expected {count} coordinates, found {value!r}")
    return tuple(float(part) for part in parts)


def _distance(a, b) -> float:
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def _dot(a, b) -> float:
    return sum(a[index] * b[index] for index in range(3))


def _sub(a, b):
    return tuple(a[index] - b[index] for index in range(3))


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalize(value):
    length = math.sqrt(_dot(value, value))
    return tuple(component / length for component in value)


def _material_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "missing"


def _prop_manifest_from_map_config(path: Path, stem: str) -> dict[str, object]:
    """Recover generated prop model paths for an incremental reconversion."""
    text = path.read_text(encoding="utf-8")
    prefix = f"csgopen/imported/{stem}/"
    models = [
        model
        for model in re.findall(r'^\s*mapmodel\s+"([^"]+)"', text, re.MULTILINE)
        if model.startswith(prefix + "props/") or model.startswith(prefix + "prop_collision/")
    ]
    if not models:
        raise SourceBspError(f"{path}: no generated prop models found for {stem}")
    return {"models": models, "counts": {"models": len(models), "reused": True}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Source 1 VBSP map")
    parser.add_argument("output", nargs="?", type=Path, help="output stage directory")
    parser.add_argument("--vpk", type=Path, help="game pak01_dir.vpk used to resolve materials")
    parser.add_argument("--scale", type=float, default=0.25)
    parser.add_argument("--displacement-lod", type=int, default=2, help="number of power-of-two LOD reductions")
    parser.add_argument("--props-manifest", type=Path, help="static-prop tile manifest emitted by sourceprops_blender.py")
    parser.add_argument(
        "--reuse-props-config",
        type=Path,
        help="existing generated map CFG whose prop model paths should be reused",
    )
    parser.add_argument(
        "--neutral-backing",
        action="store_true",
        help="experimental gray backing for collision walls; disabled by default",
    )
    args = parser.parse_args(argv)
    try:
        bsp = SourceBsp.read(args.source.read_bytes(), args.source.name)
        triangles = bsp.playable_triangles(args.displacement_lod)
        manifest = bsp.manifest(triangles, args.displacement_lod)
        if args.props_manifest and args.reuse_props_config:
            raise SourceBspError("use either --props-manifest or --reuse-props-config, not both")
        prop_manifest = None
        if args.props_manifest:
            prop_manifest = json.loads(args.props_manifest.read_text(encoding="utf-8"))
        elif args.reuse_props_config:
            prop_manifest = _prop_manifest_from_map_config(args.reuse_props_config, args.source.stem)
        if args.output:
            manifest["eclipse"] = write_eclipse_stage(
                args.source,
                bsp,
                args.output,
                args.vpk,
                args.scale,
                args.displacement_lod,
                prop_manifest,
                args.neutral_backing,
            )
        print(json.dumps(manifest, indent=2))
        return 0
    except (OSError, SourceBspError, BspError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
