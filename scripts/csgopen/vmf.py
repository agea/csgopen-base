#!/usr/bin/env python3
"""Convert Valve VMF brush geometry into a staged Eclipse Recoil map."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from q3bsp import BspError, SPAWN_CLEARANCE, _obj_position, _support_floor


TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])|([^\s{}"]+)')
PLANE = re.compile(
    r"\(([+-]?[\d.]+) ([+-]?[\d.]+) ([+-]?[\d.]+)\)\s*"
    r"\(([+-]?[\d.]+) ([+-]?[\d.]+) ([+-]?[\d.]+)\)\s*"
    r"\(([+-]?[\d.]+) ([+-]?[\d.]+) ([+-]?[\d.]+)\)"
)
SOLID_ENTITIES = {"func_detail", "func_brush", "func_wall", "func_illusionary"}
SPAWN_TEAMS = {
    "info_player_counterterrorist": "alpha",
    "info_player_terrorist": "omega",
    "info_player_start": "neutral",
    "info_player_deathmatch": "neutral",
}
TRIANGLES_PER_MESH = 100
COLLISION_TOOL_MATERIALS = {
    "tools/toolsblock_los",
    "tools/toolsblockbullets",
    "tools/toolsclip",
    "tools/toolsinvisibleladder",
    "tools/toolsplayerclip",
}


class VmfError(ValueError):
    pass


@dataclass
class Node:
    name: str
    properties: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)

    def child_nodes(self, name: str) -> Iterable["Node"]:
        return (child for child in self.children if child.name == name)


@dataclass(frozen=True)
class Side:
    points: tuple[tuple[float, float, float], ...]
    material: str


@dataclass(frozen=True)
class Spawn:
    classname: str
    team: str
    origin: tuple[float, float, float]
    yaw: float


@dataclass
class VmfMap:
    source_name: str
    roots: list[Node]
    render_triangles: list[tuple[str, tuple[tuple[float, float, float], ...]]]
    collision_triangles: list[tuple[tuple[float, float, float], ...]]
    spawns: list[Spawn]
    solid_count: int

    @classmethod
    def read(cls, text: str, source_name: str = "<memory>") -> "VmfMap":
        roots = parse_vmf(text)
        solids = []
        spawns = []
        for root in roots:
            classname = root.properties.get("classname", "").lower()
            if root.name == "world" or (root.name == "entity" and classname in SOLID_ENTITIES):
                solids.extend(root.child_nodes("solid"))
            if root.name == "entity" and classname in SPAWN_TEAMS and "origin" in root.properties:
                origin = _float_tuple(root.properties["origin"], 3)
                angles = _float_tuple(root.properties.get("angles", "0 0 0"), 3)
                spawns.append(Spawn(classname, SPAWN_TEAMS[classname], origin, angles[1]))

        render = []
        collision = []
        valid_solids = 0
        for solid in solids:
            sides = []
            for node in solid.child_nodes("side"):
                plane = node.properties.get("plane")
                if not plane:
                    continue
                sides.append(Side(_parse_plane(plane), node.properties.get("material", "missing")))
            polygons = _solid_polygons(sides)
            if not polygons:
                continue
            valid_solids += 1
            for material, triangles in polygons:
                material_lower = material.lower()
                if not material_lower.startswith("tools/") or material_lower in COLLISION_TOOL_MATERIALS:
                    collision.extend(triangles)
                if not material_lower.startswith("tools/"):
                    render.extend((material, triangle) for triangle in triangles)
        if not render:
            raise VmfError(f"{source_name}: no renderable brush faces")
        if not spawns:
            raise VmfError(f"{source_name}: no supported player starts")
        return cls(source_name, roots, render, collision, spawns, valid_solids)

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        points = [point for _, triangle in self.render_triangles for point in triangle]
        axes = list(zip(*points))
        return tuple(min(axis) for axis in axes), tuple(max(axis) for axis in axes)

    def write_render_obj(self, target) -> dict[str, int]:
        target.write(f"# VMF brush geometry from {self.source_name}\n".encode())
        triangles = [triangle for _, triangle in self.render_triangles]
        return _write_chunked_obj(target, triangles, "render")

    def write_collision_obj(self, target) -> dict[str, int]:
        target.write(f"# Outward-facing VMF brush collision from {self.source_name}\n".encode())
        return _write_chunked_obj(target, self.collision_triangles, "collision")


def _write_chunked_obj(target, triangles, prefix: str) -> dict[str, int]:
    starts = list(range(0, len(triangles), TRIANGLES_PER_MESH))
    if len(starts) > 1 and len(triangles) - starts[-1] == 1:
        starts.pop()
    starts = set(starts)
    vertex = 1
    group = 0
    for index, triangle in enumerate(triangles):
        if index in starts:
            target.write(f"g {prefix}_{group}\n".encode())
            group += 1
        for point in triangle:
            target.write(("v %.9g %.9g %.9g\n" % _obj_position(point)).encode())
        target.write(f"f {vertex} {vertex + 1} {vertex + 2}\n".encode())
        vertex += 3
    return {"vertices": vertex - 1, "triangles": len(triangles)}


def parse_vmf(text: str) -> list[Node]:
    tokens = []
    for match in TOKEN.finditer(re.sub(r"//[^\r\n]*", "", text)):
        quoted, brace, bare = match.groups()
        tokens.append(_unescape(quoted) if quoted is not None else brace or bare)
    position = 0

    def block(name: str) -> Node:
        nonlocal position
        if position >= len(tokens) or tokens[position] != "{":
            raise VmfError(f"expected '{{' after {name}")
        position += 1
        node = Node(name)
        while position < len(tokens) and tokens[position] != "}":
            key = tokens[position]
            position += 1
            if position >= len(tokens):
                raise VmfError(f"missing value for {key}")
            if tokens[position] == "{":
                node.children.append(block(key))
            else:
                node.properties[key] = tokens[position]
                position += 1
        if position >= len(tokens):
            raise VmfError(f"unterminated {name} block")
        position += 1
        return node

    roots = []
    while position < len(tokens):
        name = tokens[position]
        position += 1
        roots.append(block(name))
    return roots


def _solid_polygons(
    sides: Sequence[Side],
) -> list[tuple[str, list[tuple[tuple[float, float, float], ...]]]]:
    if len(sides) < 4:
        return []
    planes = []
    for side in sides:
        a, b, c = side.points
        normal = _cross(_sub(b, a), _sub(c, a))
        length = math.sqrt(_dot(normal, normal))
        if length < 1e-8:
            return []
        normal = tuple(value / length for value in normal)
        planes.append((normal, _dot(normal, a)))

    vertices = []
    for first, second, third in itertools.combinations(range(len(planes)), 3):
        point = _plane_intersection(planes[first], planes[second], planes[third])
        if point is None:
            continue
        # VMF side points face inward: brush volume is n.p >= distance.
        if all(_dot(normal, point) >= distance - 0.05 for normal, distance in planes):
            if not any(_distance(point, old) < 0.05 for old in vertices):
                vertices.append(point)
    if len(vertices) < 4:
        return []

    output = []
    for side, (inward, distance) in zip(sides, planes):
        face = [point for point in vertices if abs(_dot(inward, point) - distance) < 0.1]
        if len(face) < 3:
            continue
        outward = tuple(-value for value in inward)
        center = tuple(sum(point[axis] for point in face) / len(face) for axis in range(3))
        helper = (0.0, 0.0, 1.0) if abs(outward[2]) < 0.9 else (0.0, 1.0, 0.0)
        u = _normalize(_cross(helper, outward))
        v = _cross(outward, u)
        face.sort(key=lambda point: math.atan2(_dot(_sub(point, center), v), _dot(_sub(point, center), u)))
        if _dot(_cross(_sub(face[1], face[0]), _sub(face[2], face[0])), outward) < 0:
            face.reverse()
        triangles = [(face[0], face[index], face[index + 1]) for index in range(1, len(face) - 1)]
        output.append((side.material, triangles))
    return output


def write_eclipse_stage(source: Path, vmf: VmfMap, stage: Path, scale: float = 0.25) -> dict[str, object]:
    stem = source.stem.removesuffix("_d")
    model_rel = Path("csgopen") / "imported" / stem
    model_dir = stage / "data" / model_rel
    collision_rel = model_rel / "collision"
    collision_dir = stage / "data" / collision_rel
    maps_dir = stage / "data" / "maps"
    profile = stage / "profile"
    for directory in (model_dir, collision_dir, maps_dir, profile):
        directory.mkdir(parents=True, exist_ok=True)

    with (model_dir / f"{stem}.obj").open("wb") as output:
        render_stats = vmf.write_render_obj(output)
    with (collision_dir / "collision.obj").open("wb") as output:
        collision_stats = vmf.write_collision_obj(output)
    (model_dir / "obj.cfg").write_text(
        "\n".join(
            (
                f'objload "{stem}.obj"',
                'objskin * "textures/notexture.png"',
                "mdlcullface 0",
                f"mdlscale {scale * 100:.9g}",
                f'mdltricollide "{collision_rel.as_posix()}"',
            )
        ) + "\n",
        encoding="utf-8",
    )
    (collision_dir / "obj.cfg").write_text(
        f'objload "collision.obj"\nmdlscale {scale * 100:.9g}\nmdlcullface 0\n', encoding="utf-8"
    )
    (maps_dir / f"{stem}.cfg").write_text(f'mapmodel "{model_rel.as_posix()}"\n', encoding="utf-8")

    minimum, maximum = vmf.bounds()
    extents = [(maximum[i] - minimum[i]) * scale for i in range(3)]
    margin = 64.0
    required = max(value + 2 * margin for value in extents)
    world_scale = max(10, min(16, math.ceil(math.log2(max(required, 1.0)))))
    world_size = 1 << world_scale
    offset = (
        margin - minimum[0] * scale,
        margin - minimum[1] * scale,
        world_size / 2 + margin - minimum[2] * scale,
    )
    commands = [
        "vmfimport_done = 0",
        "vmfimport_finish = [",
        "    if (= $vmfimport_done 0) [",
        "        vmfimport_done = 1",
        "        edittoggle",
        f"        newmap {world_scale} {stem}",
        "        mapmodelreset 0",
        f'        exec "maps/{stem}.cfg"',
        "        newent mapmodel 0 0 0 0 100 100",
        "        entpos %.9g %.9g %.9g" % offset,
    ]
    written = 0
    for spawn in vmf.spawns:
        floor = _support_floor(spawn.origin, vmf.collision_triangles)
        if floor is None:
            continue
        team = {"neutral": 0, "alpha": 1, "omega": 2}[spawn.team]
        position = (
            spawn.origin[0] * scale + offset[0],
            spawn.origin[1] * scale + offset[1],
            floor * scale + offset[2] + SPAWN_CLEARANCE,
        )
        commands.extend(
            (
                f"        newent playerstart {team} {round(spawn.yaw) % 360} 0 0 0 {written} 0",
                "        entpos %.9g %.9g %.9g" % position,
            )
        )
        written += 1
    if not written:
        raise VmfError(f"{source}: no player starts have a supporting brush")
    commands.extend(
        (
            f'        maptitle "Imported {stem}"',
            '        mapauthor "Prototype converted from a decompiled CS:GO VMF"',
            f'        savemap "{stem}"',
            f'        echo "VMFIMPORT_DONE {stem}"',
            "        sleep 1000 [quit]",
            "    ]",
            "]",
            f'edit "{stem}"',
            "sleep 15000 [vmfimport_finish]",
        )
    )
    (profile / "build-map.cfg").write_text("\n".join(commands) + "\n", encoding="utf-8")
    (profile / "play.cfg").write_text(
        f'exec "config/csgopen/tdm.cfg"\nexec "config/csgopen/client.cfg"\ntdm {stem}\n', encoding="utf-8"
    )
    result = {
        "map": stem,
        "scale": scale,
        "world_scale": world_scale,
        "offset": offset,
        "solids": vmf.solid_count,
        "render_mesh": render_stats,
        "collision_mesh": collision_stats,
        "spawns_found": len(vmf.spawns),
        "spawns_written": written,
    }
    (stage / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _parse_plane(value: str) -> tuple[tuple[float, float, float], ...]:
    match = PLANE.fullmatch(value.strip())
    if not match:
        raise VmfError(f"invalid VMF plane: {value!r}")
    values = [float(item) for item in match.groups()]
    return tuple(tuple(values[offset : offset + 3]) for offset in range(0, 9, 3))


def _plane_intersection(first, second, third):
    n1, d1 = first
    n2, d2 = second
    n3, d3 = third
    n2xn3 = _cross(n2, n3)
    determinant = _dot(n1, n2xn3)
    if abs(determinant) < 1e-8:
        return None
    n3xn1 = _cross(n3, n1)
    n1xn2 = _cross(n1, n2)
    return tuple((d1 * n2xn3[i] + d2 * n3xn1[i] + d3 * n1xn2[i]) / determinant for i in range(3))


def _unescape(value: str) -> str:
    return value.replace(r'\"', '"').replace(r"\\", "\\")


def _float_tuple(value: str, count: int) -> tuple[float, ...]:
    parts = value.split()
    if len(parts) != count:
        raise VmfError(f"expected {count} values, found {value!r}")
    return tuple(float(part) for part in parts)


def _sub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normalize(value):
    length = math.sqrt(_dot(value, value))
    return tuple(item / length for item in value)


def _distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--scale", type=float, default=0.25)
    args = parser.parse_args(argv)
    try:
        vmf = VmfMap.read(args.source.read_text(encoding="utf-8", errors="replace"), args.source.name)
        result = write_eclipse_stage(args.source, vmf, args.stage, args.scale)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, VmfError, BspError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
