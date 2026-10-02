#!/usr/bin/env python3

import importlib.util
import io
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("vmf.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("vmf", MODULE_PATH)
vmf = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = vmf
SPEC.loader.exec_module(vmf)


class VmfTest(unittest.TestCase):
    def test_parses_nested_keyvalues(self):
        roots = vmf.parse_vmf('world { "classname" "worldspawn" solid { "id" "2" } }')
        self.assertEqual(roots[0].properties["classname"], "worldspawn")
        self.assertEqual(list(roots[0].child_nodes("solid"))[0].properties["id"], "2")

    def test_reconstructs_convex_box(self):
        sides = [
            vmf.Side(((0, 0, 0), (0, 1, 0), (0, 0, 1)), "brick"),
            vmf.Side(((8, 0, 0), (8, 0, 1), (8, 1, 0)), "brick"),
            vmf.Side(((0, 0, 0), (0, 0, 1), (1, 0, 0)), "brick"),
            vmf.Side(((0, 8, 0), (1, 8, 0), (0, 8, 1)), "brick"),
            vmf.Side(((0, 0, 0), (1, 0, 0), (0, 1, 0)), "brick"),
            vmf.Side(((0, 0, 8), (0, 1, 8), (1, 0, 8)), "brick"),
        ]
        polygons = vmf._solid_polygons(sides)
        self.assertEqual(len(polygons), 6)
        self.assertEqual(sum(len(triangles) for _, triangles in polygons), 12)

    def test_collision_obj_splits_large_meshes_before_ushort_limit(self):
        triangle = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        source = vmf.VmfMap("large", [], [], [triangle] * 201, [], 1)
        output = io.BytesIO()
        source.write_collision_obj(output)
        groups = output.getvalue().splitlines()
        self.assertEqual(sum(line.startswith(b"g collision_") for line in groups), 2)
        self.assertNotIn(b"g collision_2", groups)

    def test_render_obj_does_not_create_single_triangle_material_meshes(self):
        triangle = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        render = [(f"material_{index}", triangle) for index in range(101)]
        source = vmf.VmfMap("materials", [], render, [], [], 1)
        output = io.BytesIO()
        source.write_render_obj(output)
        groups = output.getvalue().splitlines()
        self.assertEqual(sum(line.startswith(b"g render_") for line in groups), 1)


if __name__ == "__main__":
    unittest.main()
