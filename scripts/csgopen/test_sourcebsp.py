#!/usr/bin/env python3

import importlib.util
import io
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("sourcebsp.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("sourcebsp", MODULE_PATH)
sourcebsp = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sourcebsp
SPEC.loader.exec_module(sourcebsp)


def empty_bsp(**overrides):
    values = dict(
        source_name="fixture.bsp",
        version=21,
        entities=[],
        vertices=[],
        edges=[],
        surfedges=[],
        faces=[],
        texinfo=[],
        texdata=[],
        planes=[],
        brushes=[],
        brushsides=[],
        models=[sourcebsp.BspModel((0.0,) * 3, (0.0,) * 3, (0.0,) * 3, 0, 0, 0)],
        model_brushes=[()],
        dispinfo=[],
        dispverts=[],
        static_props=[],
        pakfile=b"",
    )
    values.update(overrides)
    return sourcebsp.SourceBsp(**values)


class SourceBspTest(unittest.TestCase):
    def test_preserves_spawn_origin_team_and_converts_yaw(self):
        bsp = empty_bsp(
            entities=[
                {"classname": "info_player_counterterrorist", "origin": "1 2 3", "angles": "0 0 0"},
                {"classname": "info_player_terrorist", "origin": "4 5 6", "angles": "0 90 0"},
            ]
        )
        self.assertEqual(
            bsp.spawns()[0],
            sourcebsp.Spawn("info_player_counterterrorist", "alpha", (1.0, 2.0, 3.0), 270.0),
        )
        self.assertEqual(bsp.spawns()[1].team, "omega")
        self.assertEqual(bsp.spawns()[1].yaw, 0.0)

    def test_displacement_grid_uses_source_row_layout_and_lod(self):
        side = 4
        dispverts = []
        for row in range(side + 1):
            for column in range(side + 1):
                dispverts.append(sourcebsp.DispVertex((0.0, 0.0, 1.0), row * 10.0 + column))
        bsp = empty_bsp(dispverts=dispverts)
        polygon = [(0.0, 0.0, 0.0), (0.0, 4.0, 0.0), (4.0, 4.0, 0.0), (4.0, 0.0, 0.0)]
        points = bsp._displacement_points(
            polygon, sourcebsp.DispInfo((0.0, 0.0, 0.0), 0, 2), 1
        )
        self.assertEqual(len(points), 3)
        self.assertEqual(points[1][2], (4.0, 2.0, 24.0))

    def test_chunks_never_leave_a_single_triangle_mesh(self):
        chunks = sourcebsp._safe_chunks(list(range(201)), 100)
        self.assertEqual([len(chunk) for chunk in chunks], [100, 99, 2])
        self.assertEqual(sourcebsp._safe_chunks([7], 100), [[7, 7]])

    def test_collision_mesh_stays_below_model_index_limit(self):
        triangle = sourcebsp.Triangle(
            "test", ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), ((0.0, 0.0),) * 3
        )
        output = io.BytesIO()
        stats = sourcebsp._write_collision_obj(output, [triangle] * 100, "fixture.bsp")
        self.assertEqual(stats, {"vertices": 600, "triangles": 200})
        self.assertIn(b"v -0 0 0\nv -0 0 1\nv -1 0 0\n", output.getvalue())

    def test_collision_partitions_follow_horizontal_centroids(self):
        def triangle(x, y):
            return sourcebsp.Triangle(
                "test",
                ((x, y, 0.0), (x + 1.0, y, 0.0), (x, y + 1.0, 0.0)),
                ((0.0, 0.0),) * 3,
            )

        partitions = sourcebsp._collision_partitions(
            [triangle(0.0, 0.0), triangle(12.0, 0.0), triangle(-1.0, 0.0)], 10.0
        )
        self.assertEqual({key: len(value) for key, value in partitions.items()}, {(0, 0): 1, (1, 0): 1, (-1, 0): 1})

    def test_reconstructs_convex_brush_collision(self):
        planes = [
            sourcebsp.Plane((1.0, 0.0, 0.0), 1.0),
            sourcebsp.Plane((-1.0, 0.0, 0.0), 1.0),
            sourcebsp.Plane((0.0, 1.0, 0.0), 1.0),
            sourcebsp.Plane((0.0, -1.0, 0.0), 1.0),
            sourcebsp.Plane((0.0, 0.0, 1.0), 1.0),
            sourcebsp.Plane((0.0, 0.0, -1.0), 1.0),
        ]
        sides = [sourcebsp.BrushSide(index, 0, False) for index in range(6)]
        triangles = sourcebsp._brush_triangles(sourcebsp.Brush(0, 6, 1), sides, planes)
        self.assertEqual(len(triangles), 12)
        self.assertEqual(
            {round(coordinate) for triangle in triangles for point in triangle for coordinate in point},
            {-1, 1},
        )
        without_top = sourcebsp._brush_triangles(
            sourcebsp.Brush(0, 6, 1), sides, planes, {4}
        )
        self.assertEqual(len(without_top), 10)

    def test_positioned_entity_clears_editor_selection(self):
        commands = []
        sourcebsp._append_positioned_entity(
            commands, "newent playerstart 1 90 0 0 0 0 0", (1.0, 2.0, 3.0)
        )
        self.assertEqual(
            commands,
            [
                "        newent playerstart 1 90 0 0 0 0 0",
                "        entpos 1 2 3",
                "        entcancel",
            ],
        )

    def test_converts_dxt1_vtf_largest_mip_to_dds(self):
        header = bytearray(80)
        header[:4] = b"VTF\0"
        struct.pack_into("<II", header, 4, 7, 2)
        struct.pack_into("<I", header, 12, 80)
        struct.pack_into("<HH", header, 16, 4, 4)
        struct.pack_into("<H", header, 24, 1)
        struct.pack_into("<i", header, 52, 13)
        header[56] = 1
        struct.pack_into("<i", header, 57, -1)
        dds = sourcebsp._vtf_to_dds(bytes(header) + b"12345678")
        self.assertEqual(dds[:4], b"DDS ")
        self.assertEqual(struct.unpack_from("<II", dds, 12), (4, 4))
        self.assertEqual(dds[84:88], b"DXT1")
        self.assertEqual(dds[-8:], b"12345678")

    def test_builds_neutral_dxt1_texture(self):
        dds = sourcebsp._solid_dxt1_dds(0x8410)
        self.assertEqual(dds[:4], b"DDS ")
        self.assertEqual(struct.unpack_from("<II", dds, 12), (4, 4))
        self.assertEqual(dds[84:88], b"DXT1")
        self.assertEqual(len(dds), 136)

    def test_reads_vpk_directory_entry_and_validates_crc(self):
        payload = b"material"
        entry = struct.pack(
            "<IHHIIH", zlib.crc32(payload), 0, sourcebsp.VPK_DIR_INDEX, 0, len(payload), 0xFFFF
        )
        tree = b"vmt\0materials\0test\0" + entry + b"\0\0\0"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pak01_dir.vpk"
            path.write_bytes(
                struct.pack("<III", sourcebsp.VPK_SIGNATURE, 1, len(tree)) + tree + payload
            )
            archive = sourcebsp.VpkArchive(path)
            self.assertEqual(archive.read("materials/test.vmt"), payload)


if __name__ == "__main__":
    unittest.main()
