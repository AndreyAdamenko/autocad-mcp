import math
import unittest
from collections import Counter

from autocad_mcp.ventilation import insulation_sweep, outline_loops, rectangular_body, section_from_text, segment_distance, sweep


class VentilationGeometryTests(unittest.TestCase):
    def test_outline_faces_split_crossing_flange_strokes_and_ignore_open_stubs(self):
        paths = [[[0,-50],[0,250]], [[1000,-50],[1000,250]],
                 [[0,0],[1000,0]], [[0,200],[1000,200]], [[500,200],[500,400]]]
        loops = outline_loops(paths)
        self.assertEqual(len(loops), 1)
        self.assertEqual(len(loops[0]), 5)
        self.assertAlmostEqual(sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(loops[0],loops[0][1:]+loops[0][:1]))/2,200000)

    def test_open_outline_is_never_silently_closed(self):
        self.assertEqual(outline_loops([[[0,0],[100,0],[100,100]]]), [])

    def assertClosed(self, faces):
        edges = Counter(tuple(sorted((face[i], face[(i + 1) % len(face)]))) for face in faces for i in range(len(face)))
        self.assertTrue(all(count == 2 for count in edges.values()))

    def test_callout_profile_does_not_use_airflow_as_a_diameter(self):
        self.assertEqual(section_from_text('{\\C0;В3\\P%%C160\\PL350}'), {'shape': 'round', 'diameter_mm': 160})
        self.assertEqual(section_from_text('300х200 L1310'), {'shape': 'rectangular', 'width_mm': 300, 'height_mm': 200})
        self.assertIsNone(section_from_text('В3 L475'))

    def test_vertical_circular_duct_has_known_diameter_and_closed_caps(self):
        vertices, faces = sweep([[0, 0, 3500], [0, 0, 8570]], {'shape': 'round', 'diameter_mm': 200})
        self.assertClosed(faces)
        for i in (0, 1):
            self.assertAlmostEqual(max(p[i] for p in vertices) - min(p[i] for p in vertices), 200)
        self.assertEqual(min(p[2] for p in vertices), 3500)
        self.assertEqual(max(p[2] for p in vertices), 8570)

    def test_rectangular_duct_and_taper_dimensions_are_separate_from_length(self):
        vertices, faces = sweep([[0, 0, 3200], [1000, 0, 3200]],
                               {'shape': 'rectangular', 'width_mm': 400, 'height_mm': 300},
                               {'shape': 'rectangular', 'width_mm': 200, 'height_mm': 100})
        self.assertClosed(faces)
        self.assertEqual(max(p[1] for p in vertices[:4]) - min(p[1] for p in vertices[:4]), 400)
        self.assertEqual(max(p[2] for p in vertices[4:]) - min(p[2] for p in vertices[4:]), 100)

    def test_curved_path_does_not_create_nonfinite_vertices_or_open_edges(self):
        path = [[100 * math.cos(i * math.pi / 16), 100 * math.sin(i * math.pi / 16), 3500] for i in range(9)]
        vertices, faces = sweep(path, {'shape': 'round', 'diameter_mm': 100})
        self.assertClosed(faces)
        self.assertTrue(all(math.isfinite(v) for p in vertices for v in p))

    def test_degenerate_path_and_unknown_dimensions_are_rejected(self):
        for path, section in [([[0, 0, 0], [0, 0, 0]], {'shape': 'round', 'diameter_mm': 100}),
                              ([[0, 0, 0], [100, 0, 0]], {'shape': 'round', 'diameter_mm': -1})]:
            with self.assertRaises(ValueError):
                sweep(path, section)

    def test_flange_overhang_is_not_the_physical_duct_width(self):
        polygon, width = rectangular_body([[[0, 0, 0], [1000, 0, 0]], [[0, 250, 0], [1000, 250, 0]],
                                            [[0, -50, 0], [0, 300, 0]], [[1000, -50, 0], [1000, 300, 0]]])
        self.assertEqual(width, 250)
        self.assertEqual(len(polygon), 4)
        self.assertEqual(segment_distance([500, 300], [0, 0], [1000, 0]), (300, .5))

    def test_insulation_is_a_closed_shell_with_the_documented_outer_size(self):
        vertices, faces = insulation_sweep([[0, 0, 0], [0, 0, 1000]],
                                           {'shape': 'round', 'diameter_mm': 200}, 40)
        self.assertClosed(faces)
        self.assertAlmostEqual(max(p[0] for p in vertices) - min(p[0] for p in vertices), 280)
        inner = vertices[len(vertices) // 2:]
        self.assertAlmostEqual(max(p[0] for p in inner) - min(p[0] for p in inner), 200)

    def test_centreline_does_not_replace_the_outer_parallel_edges(self):
        polygon, width = rectangular_body([[[0, 0, 0], [1000, 0, 0]], [[0, 100, 0], [1000, 100, 0]],
                                            [[0, -100, 0], [1000, -100, 0]]], expected_width=200)
        self.assertEqual(width, 200)


if __name__ == '__main__':
    unittest.main()
