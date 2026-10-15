"""Offline geometric and read-only invariants; these do not certify AutoCAD."""
import json
import math
import os
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from autocad_mcp import acad_core
from autocad_mcp.extraction import GeometryScan, RecordAssembler, entity_geometry
from autocad_mcp.geometry import identity, insertion_matrix, multiply, nurbs_point, ocs_basis, polyline_length, sample_spline, transform, world_geometry
from autocad_mcp.interpretation import elevation_candidate, hypothesis, validate_rules


class ReadOnlyTests(unittest.TestCase):
    def test_mutators_reject_direct_handler_calls_before_com(self):
        with patch.dict(os.environ, {"ACAD_MCP_READ_ONLY": "1"}), patch.object(acad_core, "running_app") as connect:
            for name in acad_core.MUTATING_TOOLS:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "READ_ONLY"):
                    acad_core.HANDLERS[name]({})
            connect.assert_not_called()

    def test_version_rejects_wrong_unversioned_session(self):
        with patch.dict(os.environ, {"ACAD_MCP_EXPECTED_VERSION": "19.1"}), patch.object(
                acad_core.win32com.client, "GetActiveObject", return_value=SimpleNamespace(Version="25.1")):
            with self.assertRaisesRegex(RuntimeError, "received 25.1"):
                acad_core.running_app()

    def test_actual_2014_service_pack_version_is_accepted(self):
        app = SimpleNamespace(Version="19.1s (LMS Tech)")
        with patch.dict(os.environ, {"ACAD_MCP_EXPECTED_VERSION": "19.1"}), patch.object(
                acad_core.win32com.client, "GetActiveObject", return_value=app):
            self.assertIs(acad_core.running_app(), app)

    def test_legacy_2014_save_uses_supported_2013_format(self):
        # No DWG is written: only verify the marshalled AcSaveAsType.
        doc = SimpleNamespace(Application=SimpleNamespace(Version="19.1s (LMS Tech)"),
                              SaveAs=Mock(), GetVariable=lambda _:4)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ACAD_MCP_READ_ONLY":"0"}), \
                patch.object(acad_core,"OUTPUT_ROOT",Path(directory).resolve()), patch.object(acad_core,"running_app"), \
                patch.object(acad_core,"active_document",return_value=doc):
            acad_core.save_drawing_as({"path":"legacy.dxf"})
            self.assertEqual(doc.SaveAs.call_args.args[1],61)


class CoordinateTests(unittest.TestCase):
    def assertPoint(self, a, b):
        for x,y in zip(a,b):
            self.assertAlmostEqual(x, y, places=8)

    def test_nested_rotation_nonuniform_mirror_and_nonzero_origin(self):
        parent = insertion_matrix([100, 200, 30], [0,0,1], math.pi/2, [2,3,4], [1,2,3])
        child = insertion_matrix([5,6,7], [0,0,1], 0, [-1,2,1], [0,0,0])
        self.assertPoint(transform(multiply(parent, child), [2,1,0]), [82,204,46])

    def test_ocs_tilt_preserves_elevation_in_world(self):
        self.assertPoint(transform(ocs_basis([0,1,0]), [2,3,4]), [-2,4,3])

    def test_polyline_ocs_is_not_treated_as_wcs(self):
        poly = SimpleNamespace(Coordinates=(0,0,10,0), Closed=False, Normal=(0,1,0), Elevation=7,
                               GetBulge=lambda i: 0, GetWidth=lambda i: (0,0), _FlagAsMethod=lambda name: None)
        geometry = entity_geometry(poly, "AcDbPolyline")
        self.assertEqual(geometry["vertices"], [[0,7,0],[-10,7,0]])

    def test_bulge_semicircle_has_expected_bounds_and_endpoints(self):
        record = {"world_matrix": identity(), "geometry": {"kind":"polyline", "vertices":[[0,0,0],[10,0,0]],
                  "bulges":[1,0], "closed":False, "normal":[0,0,1]}}
        pts, _,_,_ = world_geometry(record, 0.01)
        self.assertPoint(pts[0], [0,0,0])
        self.assertPoint(pts[-1], [10,0,0])
        self.assertLess(abs(min(p[1] for p in pts)+5), 0.01)

    def test_repeated_vertex_with_nonzero_bulge_has_zero_length(self):
        geometry = {"kind":"polyline", "vertices":[[0,0,0],[0,0,0],[10,0,0]],
                    "closed":False, "normal":[0,0,1], "bulges":[0.52,0,0]}
        pts,*_ = world_geometry({"world_matrix":identity(), "geometry":geometry})
        self.assertEqual(polyline_length(geometry),10)
        self.assertEqual(pts[-1],[10,0,0])

    def test_nonuniform_circle_becomes_ellipse_without_losing_transform(self):
        m = insertion_matrix([10,20,0], [0,0,1], 0, [2,3,1], [0,0,0])
        pts, _,_,closed = world_geometry({"world_matrix":m, "geometry":{
            "kind":"circle", "center":[0,0,0], "radius":10, "normal":[0,0,1]}}, 0.01)
        self.assertTrue(closed)
        self.assertLess(abs(max(p[0] for p in pts)-30), 0.01)
        self.assertLess(abs(max(p[1] for p in pts)-50), 0.01)

    def test_polynomial_spline_does_not_read_nonexistent_weights(self):
        spline = SimpleNamespace(ControlPoints=(0,0,0,1,1,0,2,0,0), Knots=(0,0,0,1,1,1), IsRational=False,
                                 Degree=2, Closed=False, IsPeriodic=False)
        g = entity_geometry(spline, "AcDbSpline")
        self.assertEqual(g["weights"], [1,1,1])
        self.assertPoint(nurbs_point(g, 0.5), [1,0.5,0])

    def test_rational_quarter_circle_spline(self):
        g = {"control_points":[[1,0,0],[1,1,0],[0,1,0]], "weights":[1,math.sqrt(0.5),1],
             "knots":[0,0,0,1,1,1], "degree":2}
        self.assertPoint(nurbs_point(g,0.5), [math.sqrt(0.5),math.sqrt(0.5),0])
        pts = sample_spline(g,0.001)
        self.assertGreater(len(pts),3)
        for p in pts:
            self.assertAlmostEqual(p[0]*p[0]+p[1]*p[1],1, places=8)


class BatchTests(unittest.TestCase):
    def fake_scan(self, records):
        scan = GeometryScan.__new__(GeometryScan)
        scan.page, scan.finished, scan.pending = 0, False, None
        scan.fingerprint = lambda: "unchanged"
        scan.baseline = "unchanged"
        scan.path, scan.before_hash = __file__, "fixture"
        scan.iterator = iter(records)
        from collections import Counter
        scan.counts = Counter()
        return scan

    def test_large_single_entity_is_fragmented_without_data_loss(self):
        records = [{"id":"Model/A", "status":"ok", "payload":"текст\\\""*20000}]
        scan, assembler = self.fake_scan(records), RecordAssembler()
        restored, page_count = [], 0
        with patch("autocad_mcp.extraction.source_hash", return_value="fixture"):
            while True:
                batch = scan.batch(scan.page, 5, 4096)
                self.assertLessEqual(len(json.dumps(batch, ensure_ascii=True).encode()), 4096)
                restored.extend(assembler.accept(batch["fragments"]))
                page_count += 1
                if batch["done"]:
                    break
        self.assertGreater(page_count, 1)
        self.assertEqual(restored, records)
        self.assertFalse(assembler.pending)

    def test_drawing_changes_and_replayed_cursors_are_rejected(self):
        scan = self.fake_scan([])
        with self.assertRaisesRegex(ValueError, "Expected page"):
            scan.batch(2)
        scan.fingerprint = lambda: "edited"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            scan.batch(0)

    def test_fragment_gap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing/replayed"):
            RecordAssembler().accept([{"record_id":"A", "offset":4, "total_chars":8, "json_fragment":"abcd"}])


class InterpretationTests(unittest.TestCase):
    def test_layer_name_never_confirms_height_or_role(self):
        record = {"id":"Model/A", "layer":"Стены", "geometry":{"kind":"line"}}
        result = hypothesis(record)
        self.assertIn("Walls", result["candidates"])
        self.assertFalse(result["confirmed"])
        self.assertIn("height", result["unknown_parameters"])

    def test_elevation_number_is_not_automatically_metres(self):
        result = elevation_candidate({"id":"A", "geometry":{"kind":"text", "text":"Ур. +3,600"}})
        self.assertFalse(result["confirmed"])
        self.assertIsNone(result["units"])

    def test_solid_rule_requires_sourced_parameters(self):
        with self.assertRaisesRegex(ValueError, "height_mm"):
            validate_rules([{"category":"Walls", "layer_pattern":"Walls", "confirmed":True, "evidence":"explicit rule"}])


if __name__ == "__main__":
    unittest.main()
