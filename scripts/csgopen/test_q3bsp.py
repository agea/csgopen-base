#!/usr/bin/env python3

import importlib.util
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("q3bsp.py")
SPEC = importlib.util.spec_from_file_location("q3bsp", MODULE_PATH)
q3bsp = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = q3bsp
SPEC.loader.exec_module(q3bsp)


def record(fmt, *values):
    return struct.pack(fmt, *values)


def make_bsp(version=46):
    lumps = [b""] * q3bsp.LUMP_COUNT
    lumps[q3bsp.L_ENTITIES] = (
        b'{\n"classname" "team_CTF_redspawn"\n"origin" "1 2 3"\n"angle" "90"\n}\n\0'
    )
    lumps[q3bsp.L_TEXTURES] = record("<64sii", b"textures/test", 0, 1)
    lumps[q3bsp.L_PLANES] = record("<4f", 1, 0, 0, 8)
    lumps[q3bsp.L_BRUSHES] = record("<3i", 0, 1, 0)
    lumps[q3bsp.L_BRUSHSIDES] = record("<2i", 0, 0)
    lumps[q3bsp.L_VERTICES] = b"".join(
        record("<10f4B", x, y, z, 0.25, 0.75, 0, 0, 1, 0, 0, 255, 255, 255, 255)
        for x, y, z in ((0, 0, 0), (4, 0, 0), (0, 4, 0))
    )
    lumps[q3bsp.L_MESHVERTS] = b"".join(record("<i", value) for value in (0, 1, 2))
    face_values = [0, -1, 1, 0, 3, 0, 3, -1, 0, 0, 0, 0]
    face_values += [0.0] * 12
    face_values += [0, 0]
    lumps[q3bsp.L_FACES] = record("<12i12f2i", *face_values)

    offset = 8 + q3bsp.LUMP_COUNT * 8
    directory = []
    payload = bytearray()
    for lump in lumps:
        directory.append((offset + len(payload), len(lump)))
        payload.extend(lump)
    header = record("<4si", b"IBSP", version)
    header += b"".join(record("<ii", *entry) for entry in directory)
    return header + payload


class Q3BspTest(unittest.TestCase):
    def test_reads_compiled_geometry_collision_and_team_spawn(self):
        bsp = q3bsp.BspMap.read(make_bsp())
        self.assertEqual(bsp.spawns()[0].team, "alpha")
        self.assertEqual(bsp.spawns()[0].origin, (1.0, 2.0, 3.0))
        self.assertEqual(len(list(bsp.solid_brushes())), 1)
        target = io.BytesIO()
        stats = bsp.write_obj(target)
        self.assertEqual(stats["triangles"], 1)
        self.assertIn(b"g textures_test", target.getvalue())
        self.assertIn(b"v -4 0 0", target.getvalue())
        self.assertIn(b"f 1/1 2/2 3/3", target.getvalue())
        supported, unsupported = bsp.supported_spawns()
        self.assertEqual(supported[0][1], 0.0)
        self.assertEqual(unsupported, [])
        stacked = (
            ((0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 4.0, 0.0)),
            ((0.0, 0.0, 2.0), (4.0, 0.0, 2.0), (0.0, 4.0, 2.0)),
        )
        self.assertEqual(q3bsp._support_floor((1.0, 1.0, 3.0), stacked), 2.0)
        self.assertEqual(q3bsp._support_floor((1.0, 1.0, 3.0), stacked, "lowest"), 0.0)

    def test_reads_bsp_from_pk3_without_map_source(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "test.pk3"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("maps/only_compiled.bsp", make_bsp())
            maps = q3bsp.read_maps(archive_path)
            self.assertEqual(len(maps), 1)
            self.assertEqual(maps[0].source_name, "test.pk3:maps/only_compiled.bsp")

    def test_writes_eclipse_stage_with_native_team_spawn_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "test.pk3"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("maps/only_compiled.bsp", make_bsp())
                archive.writestr("textures/test.jpg", b"image fixture")
            bsp = q3bsp.read_maps(archive_path)[0]
            stage = root / "stage"
            conversion = q3bsp.write_eclipse_stage(archive_path, bsp, stage)
            self.assertEqual(conversion["textures_extracted"], 1)
            self.assertTrue((stage / "data/csgopen/imported/only_compiled/only_compiled.obj").is_file())
            model_config = (stage / "data/csgopen/imported/only_compiled/obj.cfg").read_text()
            self.assertIn('objskin "textures_test" "textures_test.jpg"', model_config)
            self.assertIn(
                'mdltricollide "csgopen/imported/only_compiled/collision"', model_config
            )
            collision = stage / "data/csgopen/imported/only_compiled/collision/collision.obj"
            self.assertIn("f 3 2 1", collision.read_text())
            self.assertEqual(conversion["collision_mesh"]["triangles"], 2)
            self.assertEqual(conversion["spawns_written"], 1)
            self.assertEqual(conversion["spawns_supported"], 1)
            self.assertEqual(conversion["spawns_unsupported"], 0)
            build_job = (stage / "profile/build-map.cfg").read_text()
            self.assertIn("edittoggle", build_job)
            self.assertIn("mapmodelreset 0", build_job)
            self.assertIn("newent playerstart 1 90", build_job)
            self.assertIn("entpos 64.25 64.5 588", build_job)
            self.assertIn('savemap "only_compiled"', build_job)
            self.assertIn("tdm only_compiled", (stage / "profile/play.cfg").read_text())

    def test_rejects_wrong_bsp_version(self):
        with self.assertRaisesRegex(q3bsp.BspError, "unsupported IBSP version"):
            q3bsp.BspMap.read(make_bsp(version=47))

    def test_backslash_in_entity_value_is_not_a_unicode_escape(self):
        entities = q3bsp._parse_entities(b'{"target" "\\ut4_baeza"}\0')
        self.assertEqual(entities, [{"target": "\\ut4_baeza"}])


if __name__ == "__main__":
    unittest.main()
