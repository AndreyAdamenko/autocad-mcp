# Copyright 2026 JIALE LIU
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline tests for autocad-mcp.

Everything here runs without AutoCAD installed or running: the COM surface is
faked, so the parts that can be checked on a laptop -- unit conversion, path
containment, argument validation, tool schemas -- actually are.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autocad_mcp import acad_core  # noqa: E402  (path set above)


def variant_values(variant: object) -> tuple[float, ...]:
    """Read back a point/vector the server marshalled for COM."""

    return tuple(round(value, 9) for value in variant.value)


class FlagRecorder:
    """Records _FlagAsMethod, which costs one cross-process GetIDsOfNames each.

    The real pywin32 resolves every flagged name against the live COM object, so
    the count of names flagged is the count of round trips a call pays for.
    """

    def __init__(self) -> None:
        self.flagged: list[str] = []
        self.unknown_methods: set[str] = set()

    def _FlagAsMethod(self, *names: str) -> None:
        for name in names:
            if name in self.unknown_methods:
                raise Exception(f"unknown method {name!r}")
            self.flagged.append(name)


class FakeLayer:
    """The IAcadLayer members the server reads and writes."""

    def __init__(self, name: str) -> None:
        self.Name = name
        self.Color = 7
        self.Lock = False
        self.Freeze = False
        self.LayerOn = True
        self.Linetype = "Continuous"


class FakeLayers:
    def __init__(self, names: list[str]) -> None:
        self._layers = [FakeLayer(name) for name in names]

    @property
    def Count(self) -> int:
        return len(self._layers)

    def Item(self, key: object) -> FakeLayer:
        # AutoCAD matches symbol-table names case-insensitively.
        if isinstance(key, int):
            return self._layers[key]
        for layer in self._layers:
            if layer.Name.casefold() == str(key).casefold():
                return layer
        raise Exception(f"layer {key!r} not found")

    def Add(self, name: str) -> FakeLayer:
        layer = FakeLayer(name)
        self._layers.append(layer)
        return layer


class FakeBlock:
    def __init__(self, name: str, is_layout: bool = False, is_xref: bool = False) -> None:
        self.Name = name
        self.IsLayout = is_layout
        self.IsXRef = is_xref
        self.Count = 0


class FakeBlocks:
    def __init__(self, blocks: list[FakeBlock]) -> None:
        self._blocks = list(blocks)

    @property
    def Count(self) -> int:
        return len(self._blocks)

    def Item(self, key: object) -> FakeBlock:
        if isinstance(key, int):
            return self._blocks[key]
        for block in self._blocks:
            if block.Name.casefold() == str(key).casefold():
                return block
        raise Exception(f"block {key!r} not found")


class FakeLayout(FlagRecorder):
    def __init__(
        self,
        name: str,
        tab_order: int = 0,
        model: bool = False,
        paper_units: int = 1,
        paper: tuple[float, float] | None = (420.0, 297.0),
        config: str = "None",
    ) -> None:
        super().__init__()
        self.Name = name
        self.TabOrder = tab_order
        self.ModelType = model
        self.PaperUnits = paper_units
        self.ConfigName = config
        self.CanonicalMediaName = "ISO_A3"
        self.Block = FakeBlock("*Paper_Space" if not model else "*Model_Space")
        self._paper = paper

    def GetPaperSize(self) -> tuple[float, float]:
        if self._paper is None:
            raise Exception("no plot device is configured for this layout")
        return self._paper


class FakeLayouts:
    def __init__(self, layouts: list[FakeLayout]) -> None:
        self._layouts = list(layouts)

    @property
    def Count(self) -> int:
        return len(self._layouts)

    def Item(self, index: int) -> FakeLayout:
        return self._layouts[index]


class FakeEntity(FlagRecorder):
    def __init__(self, handle: str, object_name: str, layer: str) -> None:
        super().__init__()
        self.Handle = handle
        self.ObjectName = object_name
        self.Layer = layer
        self.Color = 7
        self.Linetype = "ByLayer"
        self.Closed = False
        self.Elevation = 0.0
        self.deleted = False
        self.moves: list[tuple] = []
        self.copies = 0

    def Delete(self) -> None:
        self.deleted = True

    def Move(self, from_point: object, to_point: object) -> None:
        self.moves.append((variant_values(from_point), variant_values(to_point)))

    def Copy(self) -> "FakeEntity":
        self.copies += 1
        duplicate = FakeEntity(self.Handle + "C", self.ObjectName, self.Layer)
        self.duplicate = duplicate
        return duplicate


class CountingEntity(FakeEntity):
    """Counts property reads, because each one is a COM round trip."""

    TRACKED = ("Handle", "ObjectName", "Layer", "Color", "Linetype")

    def __init__(self, handle: str, object_name: str, layer: str) -> None:
        super().__init__(handle, object_name, layer)
        object.__setattr__(self, "reads", Counter())

    def __getattribute__(self, name: str) -> object:
        if name in CountingEntity.TRACKED:
            object.__getattribute__(self, "reads")[name] += 1
        return object.__getattribute__(self, name)


class FakeDimension(FakeEntity):
    def __init__(self, measurement: float = 0.0) -> None:
        super().__init__("D1", "AcDbRotatedDimension", "0")
        self.Measurement = measurement


class FakeSpace(FlagRecorder):
    def __init__(self, entities: list[FakeEntity] | None = None, measurement: float = 100.0) -> None:
        super().__init__()
        self._entities = entities or []
        self.added: list[tuple] = []
        self._measurement = measurement

    @property
    def Count(self) -> int:
        return len(self._entities)

    def Item(self, index: int) -> FakeEntity:
        return self._entities[index]

    def _record(self, kind: str, entity: FakeEntity, *args: object) -> FakeEntity:
        self.added.append((kind, args))
        return entity

    def AddLightWeightPolyline(self, coordinates: object) -> FakeEntity:
        return self._record("polyline", FakeEntity("2A", "AcDbPolyline", "0"), coordinates)

    def AddLine(self, start: object, end: object) -> FakeEntity:
        return self._record("line", FakeEntity("2B", "AcDbLine", "0"), start, end)

    def AddCircle(self, center: object, radius: float) -> FakeEntity:
        return self._record("circle", FakeEntity("2C", "AcDbCircle", "0"), center, radius)

    def AddText(self, text: str, point: object, height: float) -> FakeEntity:
        return self._record("text", FakeEntity("2D", "AcDbText", "0"), text, point, height)

    def AddArc(self, center: object, radius: float, start: float, end: float) -> FakeEntity:
        return self._record("arc", FakeEntity("2E", "AcDbArc", "0"), center, radius, start, end)

    def AddEllipse(self, center: object, major_axis: object, ratio: float) -> FakeEntity:
        return self._record("ellipse", FakeEntity("2F", "AcDbEllipse", "0"), center, major_axis, ratio)

    def AddPoint(self, point: object) -> FakeEntity:
        return self._record("point", FakeEntity("30", "AcDbPoint", "0"), point)

    def InsertBlock(
        self, point: object, name: str, sx: float, sy: float, sz: float, rotation: float
    ) -> FakeEntity:
        return self._record(
            "block", FakeEntity("31", "AcDbBlockReference", "0"), point, name, sx, sy, sz, rotation
        )

    def AddDimAligned(self, first: object, second: object, text_position: object) -> FakeDimension:
        return self._record(
            "dim_aligned", FakeDimension(self._measurement), first, second, text_position
        )

    def AddDimRotated(
        self, first: object, second: object, location: object, rotation: float
    ) -> FakeDimension:
        return self._record(
            "dim_rotated", FakeDimension(self._measurement), first, second, location, rotation
        )


class FakeDocument:
    """Only the members the code under test actually touches."""

    def __init__(
        self,
        insunits: int = 4,
        layers: list[str] | None = None,
        blocks: list[FakeBlock] | None = None,
        entities: list[FakeEntity] | None = None,
        layouts: list[FakeLayout] | None = None,
    ) -> None:
        self.insunits = insunits
        self.Layers = FakeLayers(layers or ["0"])
        self.ActiveLayer = self.Layers.Item(0) if self.Layers.Count else None
        self.Blocks = FakeBlocks(
            blocks
            if blocks is not None
            else [FakeBlock("*Model_Space", is_layout=True), FakeBlock("BOLT-M8")]
        )
        self.Layouts = FakeLayouts(
            layouts
            if layouts is not None
            else [FakeLayout("Model", tab_order=0, model=True), FakeLayout("Layout1", tab_order=1)]
        )
        self.ActiveLayout = self.Layouts.Item(0) if self.Layouts.Count else None
        self.saved_as: tuple[str, tuple] | None = None
        self._by_handle = {entity.Handle: entity for entity in (entities or [])}

    def GetVariable(self, name: str) -> int:
        assert name == "INSUNITS"
        return self.insunits

    def SaveAs(self, path: str, *rest: object) -> None:
        self.saved_as = (path, rest)

    def HandleToObject(self, handle: str) -> FakeEntity:
        if handle not in self._by_handle:
            raise Exception(f"no object with handle {handle!r}")
        return self._by_handle[handle]


def run_tool(
    name: str,
    arguments: dict,
    doc: FakeDocument | None = None,
    space: FakeSpace | None = None,
) -> SimpleNamespace:
    """Invoke one tool handler against the fake COM surface.

    ``space_calls`` records what each tool asked get_space for, so a test can
    check that a tool flags the method it is about to call and nothing else.
    """

    doc = FakeDocument() if doc is None else doc
    space = FakeSpace() if space is None else space
    space_calls: list[tuple] = []

    def fake_get_space(document: object, requested: object = "model", *methods: str) -> FakeSpace:
        space_calls.append((requested, methods))
        return space

    with mock.patch.object(acad_core, "running_app", return_value=object()), \
         mock.patch.object(acad_core, "active_document", return_value=doc), \
         mock.patch.object(acad_core, "get_space", side_effect=fake_get_space):
        payload = acad_core.HANDLERS[name](arguments)
    return SimpleNamespace(
        payload=payload,
        data=payload.get("data", {}),
        space=space,
        doc=doc,
        space_calls=space_calls,
    )


class UnitConversionTests(unittest.TestCase):
    """The conversion table is the one place a silent error changes every dimension."""

    # Written from the physical definitions rather than copied from the module,
    # so a wrong value in acad_core cannot agree with a wrong value here.
    MM_PER_UNIT = {
        1: 25.4,                      # inch
        2: 12 * 25.4,                 # foot
        3: 5280 * 12 * 25.4,          # mile
        4: 1.0,                       # millimetre
        5: 10.0,                      # centimetre
        6: 1000.0,                    # metre
        7: 1000.0 * 1000.0,           # kilometre
        8: 25.4e-6,                   # microinch
        9: 25.4e-3,                   # mil = a thousandth of an inch
        10: 36 * 25.4,                # yard
        11: 1e-7,                     # angstrom = 0.1 nm
        12: 1e-6,                     # nanometre
        13: 1e-3,                     # micron
        14: 100.0,                    # decimetre
        15: 10_000.0,                 # decametre
        16: 100_000.0,                # hectometre
        17: 1e12,                     # gigametre
        18: 1.495978707e14,           # astronomical unit
        19: 9.4607304725808e18,       # light year
        20: 3.0856775814913673e19,    # parsec
    }

    def test_every_insunits_code_is_covered(self) -> None:
        self.assertEqual(set(acad_core.DRAWING_UNITS_PER_MM), set(self.MM_PER_UNIT))
        self.assertEqual(set(acad_core.INSUNITS_NAMES) - {0}, set(self.MM_PER_UNIT))

    def test_every_conversion_factor_is_the_reciprocal_of_its_unit(self) -> None:
        for code, mm in sorted(self.MM_PER_UNIT.items()):
            with self.subTest(code=code, unit=acad_core.INSUNITS_NAMES[code]):
                self.assertAlmostEqual(acad_core.DRAWING_UNITS_PER_MM[code] * mm, 1.0, places=10)

    def test_a_metre_stays_a_metre_in_every_drawing_unit(self) -> None:
        """End-to-end property: convert 1000 mm out and back, get 1000 mm."""
        for code, mm in sorted(self.MM_PER_UNIT.items()):
            with self.subTest(code=code, unit=acad_core.INSUNITS_NAMES[code]):
                drawing_units = 1000.0 * acad_core.DRAWING_UNITS_PER_MM[code]
                self.assertAlmostEqual(drawing_units * mm / 1000.0, 1.0, places=9)

    def test_metric_prefixes_are_not_off_by_a_thousand(self) -> None:
        """Regression: 14-17 had been reciprocated against metres, not millimetres."""
        for code, expected in ((14, 1e-2), (15, 1e-4), (16, 1e-5), (17, 1e-12)):
            with self.subTest(code=code, unit=acad_core.INSUNITS_NAMES[code]):
                self.assertAlmostEqual(acad_core.DRAWING_UNITS_PER_MM[code], expected, places=15)

    def test_mil_is_a_thousandth_of_an_inch_not_a_micrometre(self) -> None:
        """Regression: this was 1000.0, which is the micrometre factor."""
        self.assertAlmostEqual(acad_core.DRAWING_UNITS_PER_MM[9], 1 / 0.0254, places=9)

    def test_angstrom_is_ten_million_per_millimetre(self) -> None:
        """Regression: this was 1/2.54e-8, an inch-derived number."""
        self.assertAlmostEqual(acad_core.DRAWING_UNITS_PER_MM[11] / 1e7, 1.0, places=12)

    def test_scale_lookup_falls_back_to_pass_through(self) -> None:
        self.assertEqual(acad_core.drawing_units_per_mm(FakeDocument(0)), 1.0)
        self.assertEqual(acad_core.drawing_units_per_mm(FakeDocument(99)), 1.0)

    def test_metre_drawing_scales_points(self) -> None:
        doc = FakeDocument(6)
        self.assertEqual(acad_core.drawing_units_per_mm(doc), 0.001)
        self.assertEqual(acad_core.point_mm([1250, 500], 0.001, "point"), (1.25, 0.5, 0.0))

    def test_unitless_drawing_is_reported_to_the_caller(self) -> None:
        info = acad_core.document_info(FakeDocument(0))
        self.assertEqual(info["insunits_name"], "unitless")
        self.assertIsNotNone(info["unit_warning"])
        self.assertIsNone(acad_core.document_info(FakeDocument(4))["unit_warning"])


class ArgumentValidationTests(unittest.TestCase):
    def test_point_shape_is_enforced(self) -> None:
        self.assertEqual(acad_core.point_mm([1, 2], 1.0, "p"), (1.0, 2.0, 0.0))
        self.assertEqual(acad_core.point_mm([1, 2, 3], 1.0, "p"), (1.0, 2.0, 3.0))
        for bad in ([1], [1, 2, 3, 4], "1,2", None, {"x": 1}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                acad_core.point_mm(bad, 1.0, "p")

    def test_booleans_are_not_numbers(self) -> None:
        with self.assertRaises(ValueError):
            acad_core.number(True, "radius_mm")
        with self.assertRaises(ValueError):
            acad_core.point_mm([True, 2], 1.0, "p")

    def test_output_path_rejects_escape_and_wrong_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(acad_core, "OUTPUT_ROOT", root):
                self.assertEqual(acad_core.output_path("a/b.dwg", {".dwg"}), root / "a" / "b.dwg")
                for bad in ("../outside.dwg", "a/../../outside.dwg", r"..\outside.dwg"):
                    with self.subTest(path=bad), self.assertRaises(ValueError):
                        acad_core.output_path(bad, {".dwg"})
                with self.assertRaises(ValueError):
                    acad_core.output_path("a/b.pdf", {".dwg"})
                for bad in ("", "   ", None, 5):
                    with self.subTest(path=bad), self.assertRaises(ValueError):
                        acad_core.output_path(bad, {".dwg"})

    def test_layer_is_validated_before_geometry_is_created(self) -> None:
        doc = FakeDocument(layers=["0", "MCP-GEOMETRY"])
        self.assertIsNone(acad_core.resolve_layer(doc, {}))
        self.assertEqual(acad_core.resolve_layer(doc, {"layer": " MCP-GEOMETRY "}), "MCP-GEOMETRY")
        with self.assertRaises(RuntimeError):
            acad_core.resolve_layer(doc, {"layer": "NOT-THERE"})
        with self.assertRaises(ValueError):
            acad_core.resolve_layer(doc, {"layer": "  "})


class ToolSchemaTests(unittest.TestCase):
    def test_tool_names_are_unique(self) -> None:
        names = [tool.name for tool in acad_core.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("autocad_status", names)
        self.assertIn("draw_polyline", names)

    def test_every_tool_has_a_handler_and_a_description(self) -> None:
        for tool in acad_core.TOOLS:
            with self.subTest(tool=tool.name):
                self.assertIn(tool.name, acad_core.HANDLERS)
                self.assertTrue((tool.description or "").strip())

    def test_required_arguments_are_declared_as_properties(self) -> None:
        """A required name absent from properties can never be supplied."""
        for tool in acad_core.TOOLS:
            properties = set(tool.inputSchema.get("properties", {}))
            for name in tool.inputSchema.get("required", []):
                with self.subTest(tool=tool.name, argument=name):
                    self.assertIn(name, properties)

    def test_handler_count_matches_tool_count(self) -> None:
        self.assertEqual(len(acad_core.TOOLS), len(acad_core.HANDLERS))


class ScreenshotImportTests(unittest.TestCase):
    def test_wintypes_is_imported_at_module_scope(self) -> None:
        """Regression: capture_screenshot read ctypes.wintypes without importing it.

        `import ctypes` alone does not bind the wintypes submodule, so the tool
        raised AttributeError on every single call.
        """
        self.assertTrue(hasattr(acad_core, "wintypes"))
        self.assertTrue(hasattr(acad_core.wintypes, "RECT"))


class QueryEntitiesTests(unittest.TestCase):
    ENTITIES = [
        FakeEntity("A1", "AcDbLine", "0"),
        FakeEntity("A2", "AcDbCircle", "WALLS"),
        FakeEntity("A3", "AcDbLine", "WALLS"),
        FakeEntity("A4", "AcDbText", "0"),
        FakeEntity("A5", "AcDbLine", "0"),
    ]

    def run_query(self, arguments: dict) -> dict:
        space = FakeSpace(self.ENTITIES)
        with mock.patch.object(acad_core, "running_app", return_value=object()), \
             mock.patch.object(acad_core, "active_document", return_value=FakeDocument()), \
             mock.patch.object(acad_core, "get_space", return_value=space):
            return acad_core.HANDLERS["query_entities"](arguments)

    def test_unfiltered_query_returns_everything_and_is_not_truncated(self) -> None:
        payload = self.run_query({})
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["entities"]), 5)
        self.assertFalse(payload["data"]["truncated"])

    def test_limit_below_total_reports_truncation(self) -> None:
        payload = self.run_query({"limit": 2})
        self.assertEqual(len(payload["data"]["entities"]), 2)
        self.assertTrue(payload["data"]["truncated"])
        self.assertEqual(payload["data"]["scanned"], 2)

    def test_filtered_query_that_scanned_everything_is_not_truncated(self) -> None:
        """Regression: truncated was result-count < total, so every filtered
        query claimed there was more to fetch even after a full scan."""
        payload = self.run_query({"object_name": "AcDbLine"})
        self.assertEqual(len(payload["data"]["entities"]), 3)
        self.assertEqual(payload["data"]["total_in_space"], 5)
        self.assertEqual(payload["data"]["scanned"], 5)
        self.assertFalse(payload["data"]["truncated"])

    def test_layer_filter(self) -> None:
        payload = self.run_query({"layer": "WALLS"})
        handles = [entity["handle"] for entity in payload["data"]["entities"]]
        self.assertEqual(handles, ["A2", "A3"])

    def test_limit_is_range_checked(self) -> None:
        for bad in (0, -1, 1001):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.run_query({"limit": bad})


class PolylineTests(unittest.TestCase):
    def draw(self, arguments: dict) -> dict:
        space = FakeSpace()
        with mock.patch.object(acad_core, "running_app", return_value=object()), \
             mock.patch.object(acad_core, "active_document", return_value=FakeDocument(4)), \
             mock.patch.object(acad_core, "get_space", return_value=space):
            payload = acad_core.HANDLERS["draw_polyline"](arguments)
        return payload

    def test_planar_polyline_is_accepted(self) -> None:
        payload = self.draw({"vertices_mm": [[0, 0], [100, 0], [100, 60]], "closed": True})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["vertices"], 3)
        self.assertEqual(payload["data"]["elevation_mm"], 0.0)

    def test_constant_z_becomes_the_elevation(self) -> None:
        payload = self.draw({"vertices_mm": [[0, 0, 25], [100, 0, 25]]})
        self.assertEqual(payload["data"]["elevation_mm"], 25.0)

    def test_varying_z_is_refused_rather_than_flattened(self) -> None:
        """A lightweight polyline is planar; dropping the z would move geometry."""
        with self.assertRaises(ValueError):
            self.draw({"vertices_mm": [[0, 0, 0], [100, 0, 40]]})

    def test_two_vertices_are_required(self) -> None:
        with self.assertRaises(ValueError):
            self.draw({"vertices_mm": [[0, 0]]})


class SaveAsTests(unittest.TestCase):
    def save(self, relative: str) -> FakeDocument:
        doc = FakeDocument()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(acad_core, "OUTPUT_ROOT", root), \
                 mock.patch.object(acad_core, "running_app", return_value=object()), \
                 mock.patch.object(acad_core, "active_document", return_value=doc):
                acad_core.HANDLERS["save_drawing_as"]({"path": relative})
        return doc

    def test_dxf_target_passes_the_dxf_file_type(self) -> None:
        """Regression: SaveAs defaults to DWG, so a .dxf path produced a DWG file."""
        doc = self.save("out/plan.dxf")
        self.assertIsNotNone(doc.saved_as)
        self.assertTrue(doc.saved_as[0].lower().endswith(".dxf"))
        self.assertEqual(doc.saved_as[1], (acad_core.AC_2018_DXF,))

    def test_dwg_target_keeps_the_native_default(self) -> None:
        doc = self.save("out/plan.dwg")
        self.assertEqual(doc.saved_as[1], ())

    def test_overwrite_is_refused_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            existing = root / "taken.dwg"
            existing.write_bytes(b"")
            with mock.patch.object(acad_core, "OUTPUT_ROOT", root), \
                 mock.patch.object(acad_core, "running_app", return_value=object()), \
                 mock.patch.object(acad_core, "active_document", return_value=FakeDocument()):
                with self.assertRaises(RuntimeError):
                    acad_core.HANDLERS["save_drawing_as"]({"path": "taken.dwg"})
                acad_core.HANDLERS["save_drawing_as"]({"path": "taken.dwg", "allow_overwrite": True})


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_failure_names_every_progid_it_tried(self) -> None:
        """Regression: the per-ProgID errors were collected and then dropped,
        hiding the difference between 'not running' and 'COM registration broken'."""
        with mock.patch.object(acad_core.win32com.client, "GetActiveObject",
                               side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError) as caught:
                acad_core.running_app()
        message = str(caught.exception)
        for prog_id in acad_core.PROG_IDS:
            self.assertIn(prog_id, message)
        self.assertIn("boom", message)


class ScalarHelperTests(unittest.TestCase):
    def test_number_rejects_nan_and_infinity(self) -> None:
        """json.loads accepts NaN/Infinity, so a client really can send them."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                acad_core.number(bad, "radius_mm")

    def test_positive_number_rejects_zero_and_negatives(self) -> None:
        self.assertEqual(acad_core.positive_number(2.5, "radius_mm"), 2.5)
        for bad in (0, -1, -0.0001):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                acad_core.positive_number(bad, "radius_mm")

    def test_boolean_refuses_truthy_stand_ins(self) -> None:
        """bool("false") is True, which would set the opposite of what was asked."""
        self.assertIs(acad_core.boolean(False, "frozen"), False)
        for bad in ("false", "true", 0, 1, None, ""):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                acad_core.boolean(bad, "frozen")

    def test_aci_color_range(self) -> None:
        self.assertEqual(acad_core.aci_color(1), 1)
        self.assertEqual(acad_core.aci_color(255), 255)
        for bad in (0, 256, -1, True, 4.5, "4", None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                acad_core.aci_color(bad)


class SpaceCountingDocument:
    """Counts space property accesses, to show get_space does not cache them."""

    def __init__(self) -> None:
        self.model_space_reads = 0
        self.paper_space_reads = 0
        self.handed_out: list[FakeSpace] = []

    def _fresh(self) -> FakeSpace:
        space = FakeSpace()
        self.handed_out.append(space)
        return space

    @property
    def ModelSpace(self) -> FakeSpace:
        self.model_space_reads += 1
        return self._fresh()

    @property
    def PaperSpace(self) -> FakeSpace:
        self.paper_space_reads += 1
        return self._fresh()


class GetSpaceTests(unittest.TestCase):
    """Each flagged name is a GetIDsOfNames round trip, so the count matters."""

    def test_only_the_requested_methods_are_flagged(self) -> None:
        doc = SpaceCountingDocument()
        space = acad_core.get_space(doc, "model", "AddArc")
        self.assertEqual(space.flagged, ["AddArc"])

    def test_nothing_is_flagged_when_no_method_is_needed(self) -> None:
        """Regression: four names were flagged on every call, including
        query_entities, which calls none of them."""
        doc = SpaceCountingDocument()
        space = acad_core.get_space(doc, "model")
        self.assertEqual(space.flagged, [])

    def test_paper_space_is_selected_and_flagged_the_same_way(self) -> None:
        doc = SpaceCountingDocument()
        space = acad_core.get_space(doc, "paper", "AddText")
        self.assertEqual(doc.paper_space_reads, 1)
        self.assertEqual(doc.model_space_reads, 0)
        self.assertEqual(space.flagged, ["AddText"])

    def test_the_space_is_re_read_on_every_call(self) -> None:
        """Not cached on purpose: the user can close or switch the drawing from
        the AutoCAD UI between calls, and a stale wrapper would then edit the
        wrong drawing while still reporting success."""
        doc = SpaceCountingDocument()
        first = acad_core.get_space(doc, "model")
        second = acad_core.get_space(doc, "model")
        self.assertEqual(doc.model_space_reads, 2)
        self.assertIsNot(first, second)

    def test_space_name_is_validated(self) -> None:
        doc = SpaceCountingDocument()
        for bad in ("sheet", "MODEL SPACE", "3d"):
            with self.subTest(space=bad), self.assertRaises(ValueError):
                acad_core.get_space(doc, bad)
        self.assertEqual(doc.model_space_reads, 0)

    def test_one_unknown_name_does_not_cancel_the_others(self) -> None:
        """_FlagAsMethod(a, b, c) abandons b and c when a fails, so the names
        are flagged one at a time."""
        space = FakeSpace()
        space.unknown_methods = {"Nope"}
        acad_core.flag_methods(space, "Nope", "AddLine", "AddCircle")
        self.assertEqual(space.flagged, ["AddLine", "AddCircle"])


class QueryEntitiesScanTests(unittest.TestCase):
    def entities(self, count: int, object_name: str = "AcDbLine") -> list[FakeEntity]:
        return [FakeEntity(f"H{index}", object_name, "0") for index in range(count)]

    def query(self, arguments: dict, entities: list[FakeEntity]) -> dict:
        return run_tool("query_entities", arguments, space=FakeSpace(entities)).data

    def test_max_scan_caps_the_walk_and_reports_it(self) -> None:
        data = self.query({"max_scan": 10, "object_name": "AcDbCircle"}, self.entities(100))
        self.assertEqual(data["entities"], [])
        self.assertEqual(data["scanned"], 10)
        self.assertEqual(data["total_in_space"], 100)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["stopped_by"], "max_scan")

    def test_stopped_by_names_the_limit_when_the_limit_bit_first(self) -> None:
        data = self.query({"limit": 3, "max_scan": 50}, self.entities(100))
        self.assertEqual(len(data["entities"]), 3)
        self.assertEqual(data["stopped_by"], "limit")

    def test_a_complete_scan_reports_no_stop_reason(self) -> None:
        data = self.query({"object_name": "AcDbCircle"}, self.entities(5))
        self.assertEqual(data["scanned"], 5)
        self.assertFalse(data["truncated"])
        self.assertIsNone(data["stopped_by"])

    def test_max_scan_larger_than_the_space_changes_nothing(self) -> None:
        data = self.query({"max_scan": 5000}, self.entities(4))
        self.assertEqual(len(data["entities"]), 4)
        self.assertFalse(data["truncated"])

    def test_max_scan_is_range_checked(self) -> None:
        for bad in (0, -1, acad_core.MAX_SCAN_CEILING + 1):
            with self.subTest(max_scan=bad), self.assertRaises(ValueError):
                self.query({"max_scan": bad}, self.entities(2))

    def test_each_property_is_read_once_per_entity(self) -> None:
        """Regression: filtering re-read ObjectName and Layer that entity_info
        then read again, so a matching entity cost seven round trips, not five."""
        rejected = CountingEntity("A1", "AcDbLine", "0")
        matched = CountingEntity("A2", "AcDbCircle", "WALLS")
        self.query({"object_name": "AcDbCircle"}, [rejected, matched])

        self.assertEqual(rejected.reads["ObjectName"], 1)
        self.assertEqual(rejected.reads["Layer"], 0, "a rejected entity should not be read further")
        for name in ("ObjectName", "Layer", "Handle", "Color", "Linetype"):
            with self.subTest(property=name):
                self.assertEqual(matched.reads[name], 1)

    def test_query_asks_get_space_to_flag_nothing(self) -> None:
        run = run_tool("query_entities", {}, space=FakeSpace(self.entities(1)))
        self.assertEqual(run.space_calls, [("model", ())])


class DrawArcTests(unittest.TestCase):
    def test_arc_is_created_with_radian_angles(self) -> None:
        run = run_tool(
            "draw_arc",
            {"center_mm": [10, 20], "radius_mm": 50, "start_angle_deg": 0, "end_angle_deg": 90},
        )
        kind, args = run.space.added[0]
        self.assertEqual(kind, "arc")
        self.assertEqual(variant_values(args[0]), (10.0, 20.0, 0.0))
        self.assertEqual(args[1], 50.0)
        self.assertAlmostEqual(args[2], 0.0, places=12)
        self.assertAlmostEqual(args[3], math.pi / 2, places=12)
        self.assertEqual(run.data["sweep_deg"], 90.0)

    def test_sweep_is_measured_counter_clockwise_across_zero(self) -> None:
        run = run_tool(
            "draw_arc",
            {"center_mm": [0, 0], "radius_mm": 5, "start_angle_deg": 350, "end_angle_deg": 10},
        )
        self.assertEqual(run.data["sweep_deg"], 20.0)

    def test_millimetres_become_drawing_units(self) -> None:
        run = run_tool(
            "draw_arc",
            {"center_mm": [1000, 0], "radius_mm": 500, "start_angle_deg": 0, "end_angle_deg": 180},
            doc=FakeDocument(6),
        )
        _, args = run.space.added[0]
        self.assertEqual(variant_values(args[0]), (1.0, 0.0, 0.0))
        self.assertEqual(args[1], 0.5)

    def test_zero_sweep_is_refused(self) -> None:
        for start, end in ((45, 45), (0, 360), (-30, 330)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                run_tool(
                    "draw_arc",
                    {"center_mm": [0, 0], "radius_mm": 5, "start_angle_deg": start, "end_angle_deg": end},
                )

    def test_radius_and_angles_are_validated(self) -> None:
        base = {"center_mm": [0, 0], "radius_mm": 5, "start_angle_deg": 0, "end_angle_deg": 90}
        for override in (
            {"radius_mm": 0},
            {"radius_mm": -3},
            {"radius_mm": "big"},
            {"start_angle_deg": None},
            {"end_angle_deg": "90deg"},
            {"center_mm": [0]},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                run_tool("draw_arc", {**base, **override})

    def test_a_bad_layer_creates_nothing(self) -> None:
        space = FakeSpace()
        with self.assertRaises(RuntimeError):
            run_tool(
                "draw_arc",
                {
                    "center_mm": [0, 0],
                    "radius_mm": 5,
                    "start_angle_deg": 0,
                    "end_angle_deg": 90,
                    "layer": "MISSING",
                },
                space=space,
            )
        self.assertEqual(space.added, [])


class DrawEllipseTests(unittest.TestCase):
    def test_major_axis_is_marshalled_as_a_vector_from_the_centre(self) -> None:
        run = run_tool(
            "draw_ellipse",
            {"center_mm": [0, 0], "major_axis_mm": 100, "minor_axis_mm": 50},
        )
        kind, args = run.space.added[0]
        self.assertEqual(kind, "ellipse")
        self.assertEqual(variant_values(args[0]), (0.0, 0.0, 0.0))
        self.assertEqual(variant_values(args[1]), (100.0, 0.0, 0.0))
        self.assertEqual(args[2], 0.5)
        self.assertEqual(run.data["radius_ratio"], 0.5)

    def test_rotation_turns_the_major_axis_vector(self) -> None:
        run = run_tool(
            "draw_ellipse",
            {"center_mm": [0, 0], "major_axis_mm": 100, "minor_axis_mm": 25, "rotation_deg": 90},
        )
        _, args = run.space.added[0]
        self.assertEqual(variant_values(args[1]), (0.0, 100.0, 0.0))
        self.assertEqual(run.data["rotation_deg"], 90.0)

    def test_axis_lengths_are_converted_to_drawing_units(self) -> None:
        run = run_tool(
            "draw_ellipse",
            {"center_mm": [0, 0], "major_axis_mm": 2000, "minor_axis_mm": 1000},
            doc=FakeDocument(6),
        )
        _, args = run.space.added[0]
        self.assertEqual(variant_values(args[1]), (2.0, 0.0, 0.0))
        self.assertEqual(args[2], 0.5)

    def test_minor_axis_may_not_exceed_the_major_axis(self) -> None:
        """AutoCAD's RadiusRatio is minor/major and cannot exceed 1."""
        with self.assertRaises(ValueError):
            run_tool(
                "draw_ellipse",
                {"center_mm": [0, 0], "major_axis_mm": 30, "minor_axis_mm": 60},
            )

    def test_axis_lengths_must_be_positive(self) -> None:
        for override in ({"major_axis_mm": 0}, {"minor_axis_mm": 0}, {"minor_axis_mm": -5}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                run_tool(
                    "draw_ellipse",
                    {"center_mm": [0, 0], "major_axis_mm": 50, "minor_axis_mm": 25, **override},
                )


class DrawPointTests(unittest.TestCase):
    def test_point_is_scaled_and_created(self) -> None:
        run = run_tool("draw_point", {"point_mm": [250, 500]}, doc=FakeDocument(5))
        kind, args = run.space.added[0]
        self.assertEqual(kind, "point")
        self.assertEqual(variant_values(args[0]), (25.0, 50.0, 0.0))

    def test_the_point_shape_is_validated(self) -> None:
        for bad in ([1], [1, 2, 3, 4], "1,2", None):
            with self.subTest(point=bad), self.assertRaises(ValueError):
                run_tool("draw_point", {"point_mm": bad})

    def test_a_bad_layer_creates_nothing(self) -> None:
        space = FakeSpace()
        with self.assertRaises(RuntimeError):
            run_tool("draw_point", {"point_mm": [0, 0], "layer": "MISSING"}, space=space)
        self.assertEqual(space.added, [])


class InsertBlockTests(unittest.TestCase):
    def test_block_reference_is_inserted(self) -> None:
        run = run_tool(
            "insert_block",
            {"block_name": "BOLT-M8", "insertion_point_mm": [10, 20], "rotation_deg": 90},
        )
        kind, args = run.space.added[0]
        self.assertEqual(kind, "block")
        self.assertEqual(variant_values(args[0]), (10.0, 20.0, 0.0))
        self.assertEqual(args[1], "BOLT-M8")
        self.assertEqual(args[2:5], (1.0, 1.0, 1.0))
        self.assertAlmostEqual(args[5], math.pi / 2, places=12)

    def test_scale_is_unitless_while_the_insertion_point_is_not(self) -> None:
        """The block's geometry is already in drawing units, so scale is a plain
        multiplier; only the insertion point crosses the millimetre boundary."""
        run = run_tool(
            "insert_block",
            {"block_name": "BOLT-M8", "insertion_point_mm": [1000, 0], "scale": 2},
            doc=FakeDocument(6),
        )
        _, args = run.space.added[0]
        self.assertEqual(variant_values(args[0]), (1.0, 0.0, 0.0))
        self.assertEqual(args[2:5], (2.0, 2.0, 2.0))

    def test_an_unknown_block_inserts_nothing(self) -> None:
        space = FakeSpace()
        with self.assertRaises(RuntimeError):
            run_tool(
                "insert_block",
                {"block_name": "NOT-THERE", "insertion_point_mm": [0, 0]},
                space=space,
            )
        self.assertEqual(space.added, [])

    def test_a_layout_block_is_refused(self) -> None:
        """*Model_Space is model space itself, not something to insert."""
        with self.assertRaises(ValueError):
            run_tool("insert_block", {"block_name": "*Model_Space", "insertion_point_mm": [0, 0]})

    def test_arguments_are_validated(self) -> None:
        base = {"block_name": "BOLT-M8", "insertion_point_mm": [0, 0]}
        for override in (
            {"scale": 0},
            {"scale": "double"},
            {"block_name": ""},
            {"block_name": "   "},
            {"block_name": 7},
            {"rotation_deg": float("inf")},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                run_tool("insert_block", {**base, **override})


class LinearDimensionTests(unittest.TestCase):
    BASE = {"start_mm": [0, 0], "end_mm": [100, 0], "dimension_line_point_mm": [50, 25]}

    def test_aligned_uses_add_dim_aligned_and_reports_the_measurement(self) -> None:
        run = run_tool("add_linear_dimension", dict(self.BASE))
        kind, args = run.space.added[0]
        self.assertEqual(kind, "dim_aligned")
        self.assertEqual(variant_values(args[0]), (0.0, 0.0, 0.0))
        self.assertEqual(variant_values(args[1]), (100.0, 0.0, 0.0))
        self.assertEqual(variant_values(args[2]), (50.0, 25.0, 0.0))
        self.assertEqual(run.data["measurement_mm"], 100.0)
        self.assertIsNone(run.data["rotation_deg"])

    def test_horizontal_and_vertical_are_rotated_dimensions(self) -> None:
        for orientation, radians in (("horizontal", 0.0), ("vertical", math.pi / 2)):
            with self.subTest(orientation=orientation):
                run = run_tool(
                    "add_linear_dimension", {**self.BASE, "orientation": orientation}
                )
                kind, args = run.space.added[0]
                self.assertEqual(kind, "dim_rotated")
                self.assertAlmostEqual(args[3], radians, places=12)

    def test_rotated_uses_the_supplied_angle(self) -> None:
        run = run_tool(
            "add_linear_dimension",
            {**self.BASE, "orientation": "rotated", "rotation_deg": 30},
        )
        _, args = run.space.added[0]
        self.assertAlmostEqual(args[3], math.radians(30), places=12)
        self.assertEqual(run.data["rotation_deg"], 30.0)

    def test_the_measurement_is_converted_back_to_millimetres(self) -> None:
        run = run_tool(
            "add_linear_dimension",
            {"start_mm": [0, 0], "end_mm": [1000, 0], "dimension_line_point_mm": [500, 250]},
            doc=FakeDocument(6),
            space=FakeSpace(measurement=1.0),
        )
        self.assertEqual(run.data["measurement_mm"], 1000.0)

    def test_rotated_requires_an_angle(self) -> None:
        with self.assertRaises(ValueError):
            run_tool("add_linear_dimension", {**self.BASE, "orientation": "rotated"})

    def test_an_angle_with_a_fixed_orientation_is_refused(self) -> None:
        """Ignoring it would silently measure along an axis nobody asked for."""
        for orientation in ("aligned", "horizontal", "vertical"):
            with self.subTest(orientation=orientation), self.assertRaises(ValueError):
                run_tool(
                    "add_linear_dimension",
                    {**self.BASE, "orientation": orientation, "rotation_deg": 45},
                )

    def test_the_two_measured_points_must_differ(self) -> None:
        with self.assertRaises(ValueError):
            run_tool(
                "add_linear_dimension",
                {"start_mm": [5, 5], "end_mm": [5, 5, 0], "dimension_line_point_mm": [0, 0]},
            )

    def test_orientation_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            run_tool("add_linear_dimension", {**self.BASE, "orientation": "diagonal"})

    def test_a_bad_layer_creates_nothing(self) -> None:
        space = FakeSpace()
        with self.assertRaises(RuntimeError):
            run_tool("add_linear_dimension", {**self.BASE, "layer": "MISSING"}, space=space)
        self.assertEqual(space.added, [])


class EraseEntityTests(unittest.TestCase):
    def document(self) -> FakeDocument:
        return FakeDocument(entities=[FakeEntity("A1", "AcDbLine", "WALLS")])

    def test_entity_is_deleted_and_described_first(self) -> None:
        """The description has to be read before Delete: afterwards the wrapper
        points at an object that is gone and every property read raises."""
        doc = self.document()
        run = run_tool("erase_entity", {"handle": "A1"}, doc=doc)
        self.assertTrue(run.payload["ok"])
        self.assertTrue(doc.HandleToObject("A1").deleted)
        self.assertEqual(run.data["entity"]["handle"], "A1")
        self.assertEqual(run.data["entity"]["layer"], "WALLS")

    def test_delete_is_flagged_as_a_method(self) -> None:
        doc = self.document()
        run_tool("erase_entity", {"handle": "A1"}, doc=doc)
        self.assertEqual(doc.HandleToObject("A1").flagged, ["Delete"])

    def test_an_unknown_handle_is_reported(self) -> None:
        with self.assertRaises(RuntimeError):
            run_tool("erase_entity", {"handle": "ZZZ"}, doc=self.document())

    def test_an_empty_handle_is_rejected(self) -> None:
        for bad in ("", "   ", None, 5):
            with self.subTest(handle=bad), self.assertRaises(ValueError):
                run_tool("erase_entity", {"handle": bad}, doc=self.document())


class MoveEntityTests(unittest.TestCase):
    def document(self, insunits: int = 4) -> FakeDocument:
        return FakeDocument(insunits, entities=[FakeEntity("A1", "AcDbLine", "0")])

    def test_entity_is_moved_and_the_displacement_reported(self) -> None:
        doc = self.document()
        run = run_tool("move_entity", {"handle": "A1", "from_mm": [0, 0], "to_mm": [30, 40]}, doc=doc)
        entity = doc.HandleToObject("A1")
        self.assertEqual(entity.moves, [((0.0, 0.0, 0.0), (30.0, 40.0, 0.0))])
        self.assertEqual(run.data["displacement_mm"], [30.0, 40.0, 0.0])

    def test_points_are_converted_but_the_report_stays_in_millimetres(self) -> None:
        doc = self.document(6)
        run = run_tool(
            "move_entity", {"handle": "A1", "from_mm": [0, 0], "to_mm": [1000, 2000]}, doc=doc
        )
        self.assertEqual(doc.HandleToObject("A1").moves, [((0.0, 0.0, 0.0), (1.0, 2.0, 0.0))])
        self.assertEqual(run.data["displacement_mm"], [1000.0, 2000.0, 0.0])

    def test_bad_coordinates_never_reach_the_entity(self) -> None:
        doc = self.document()
        with self.assertRaises(ValueError):
            run_tool("move_entity", {"handle": "A1", "from_mm": [0], "to_mm": [1, 2]}, doc=doc)
        self.assertEqual(doc.HandleToObject("A1").moves, [])

    def test_an_unknown_handle_is_reported(self) -> None:
        with self.assertRaises(RuntimeError):
            run_tool(
                "move_entity", {"handle": "ZZZ", "from_mm": [0, 0], "to_mm": [1, 1]}, doc=self.document()
            )


class CopyEntityTests(unittest.TestCase):
    def document(self, insunits: int = 4) -> FakeDocument:
        return FakeDocument(insunits, entities=[FakeEntity("A1", "AcDbCircle", "WALLS")])

    def test_copy_without_a_displacement_is_not_moved(self) -> None:
        doc = self.document()
        run = run_tool("copy_entity", {"handle": "A1"}, doc=doc)
        source = doc.HandleToObject("A1")
        self.assertEqual(source.copies, 1)
        self.assertEqual(source.duplicate.moves, [])
        self.assertEqual(run.data["source"]["handle"], "A1")
        self.assertEqual(run.data["entity"]["handle"], "A1C")

    def test_a_displacement_moves_the_copy_from_the_origin(self) -> None:
        doc = self.document(6)
        run_tool("copy_entity", {"handle": "A1", "displacement_mm": [1000, 0]}, doc=doc)
        duplicate = doc.HandleToObject("A1").duplicate
        self.assertEqual(duplicate.moves, [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))])

    def test_a_zero_displacement_skips_the_move_call(self) -> None:
        doc = self.document()
        run_tool("copy_entity", {"handle": "A1", "displacement_mm": [0, 0]}, doc=doc)
        self.assertEqual(doc.HandleToObject("A1").duplicate.moves, [])

    def test_a_bad_displacement_never_copies(self) -> None:
        doc = self.document()
        with self.assertRaises(ValueError):
            run_tool("copy_entity", {"handle": "A1", "displacement_mm": [1, 2, 3, 4]}, doc=doc)
        self.assertEqual(doc.HandleToObject("A1").copies, 0)

    def test_an_unknown_handle_is_reported(self) -> None:
        with self.assertRaises(RuntimeError):
            run_tool("copy_entity", {"handle": "ZZZ"}, doc=self.document())


class SetLayerPropertiesTests(unittest.TestCase):
    def document(self) -> FakeDocument:
        return FakeDocument(layers=["0", "WALLS"])

    def run_it(self, arguments: dict, doc: FakeDocument) -> dict:
        with mock.patch.object(acad_core, "running_app", return_value=object()), \
             mock.patch.object(acad_core, "active_document", return_value=doc):
            return acad_core.HANDLERS["set_layer_properties"](arguments)

    def test_every_property_is_applied(self) -> None:
        doc = self.document()
        payload = self.run_it(
            {"name": "WALLS", "color_aci": 3, "locked": True, "frozen": True, "visible": False}, doc
        )
        layer = doc.Layers.Item("WALLS")
        self.assertEqual(layer.Color, 3)
        self.assertTrue(layer.Lock)
        self.assertTrue(layer.Freeze)
        self.assertFalse(layer.LayerOn)
        self.assertEqual(
            sorted(payload["data"]["changed"]), ["color_aci", "frozen", "locked", "visible"]
        )

    def test_only_the_named_properties_change(self) -> None:
        doc = self.document()
        self.run_it({"name": "WALLS", "locked": True}, doc)
        layer = doc.Layers.Item("WALLS")
        self.assertTrue(layer.Lock)
        self.assertFalse(layer.Freeze)
        self.assertTrue(layer.LayerOn)
        self.assertEqual(layer.Color, 7)

    def test_freezing_the_current_layer_is_refused_before_anything_changes(self) -> None:
        """AutoCAD cannot freeze the current layer, and its COM error says
        neither which layer nor why."""
        doc = self.document()
        doc.ActiveLayer = doc.Layers.Item("WALLS")
        with self.assertRaises(RuntimeError):
            self.run_it({"name": "WALLS", "color_aci": 5, "frozen": True}, doc)
        self.assertEqual(doc.Layers.Item("WALLS").Color, 7)
        self.assertFalse(doc.Layers.Item("WALLS").Freeze)

    def test_the_current_layer_guard_ignores_case(self) -> None:
        """AutoCAD matches layer names case-insensitively, so 'walls' and
        'WALLS' are one layer and the freeze guard must still fire."""
        doc = self.document()
        doc.ActiveLayer = doc.Layers.Item("WALLS")
        with self.assertRaises(RuntimeError):
            self.run_it({"name": "walls", "frozen": True}, doc)
        self.assertFalse(doc.Layers.Item("WALLS").Freeze)

    def test_thawing_the_current_layer_is_allowed(self) -> None:
        doc = self.document()
        doc.ActiveLayer = doc.Layers.Item("WALLS")
        self.run_it({"name": "WALLS", "frozen": False}, doc)
        self.assertFalse(doc.Layers.Item("WALLS").Freeze)

    def test_no_properties_is_refused_rather_than_a_silent_no_op(self) -> None:
        with self.assertRaises(ValueError):
            self.run_it({"name": "WALLS"}, self.document())

    def test_an_unknown_layer_is_reported(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_it({"name": "NOT-THERE", "locked": True}, self.document())

    def test_bad_values_leave_the_layer_untouched(self) -> None:
        for override in (
            {"color_aci": 0},
            {"color_aci": 300},
            {"color_aci": "red"},
            {"locked": "yes"},
            {"frozen": 1},
            {"visible": None},
        ):
            doc = self.document()
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.run_it({"name": "WALLS", **override}, doc)
            layer = doc.Layers.Item("WALLS")
            self.assertEqual((layer.Color, layer.Lock, layer.Freeze, layer.LayerOn), (7, False, False, True))

    def test_an_empty_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.run_it({"name": "   ", "locked": True}, self.document())


class ListLayoutsTests(unittest.TestCase):
    def run_it(self, doc: FakeDocument) -> dict:
        with mock.patch.object(acad_core, "running_app", return_value=object()), \
             mock.patch.object(acad_core, "active_document", return_value=doc):
            return acad_core.HANDLERS["list_layouts"]({})["data"]

    def test_layouts_are_listed_in_tab_order_with_the_active_one_marked(self) -> None:
        doc = FakeDocument(
            layouts=[
                FakeLayout("Layout2", tab_order=2),
                FakeLayout("Model", tab_order=0, model=True),
                FakeLayout("Layout1", tab_order=1),
            ]
        )
        doc.ActiveLayout = doc.Layouts.Item(2)
        data = self.run_it(doc)
        self.assertEqual([item["name"] for item in data["layouts"]], ["Model", "Layout1", "Layout2"])
        self.assertEqual(data["active_layout"], "Layout1")
        self.assertEqual([item["active"] for item in data["layouts"]], [False, True, False])
        self.assertEqual([item["is_model_tab"] for item in data["layouts"]], [True, False, False])

    def test_millimetre_paper_is_reported_unchanged(self) -> None:
        doc = FakeDocument(layouts=[FakeLayout("Layout1", paper_units=1, paper=(420.0, 297.0))])
        paper = self.run_it(doc)["layouts"][0]["paper"]
        self.assertEqual((paper["width_mm"], paper["height_mm"]), (420.0, 297.0))
        self.assertEqual(paper["paper_units"], "millimetres")

    def test_inch_paper_is_converted(self) -> None:
        doc = FakeDocument(layouts=[FakeLayout("Layout1", paper_units=0, paper=(11.0, 8.5))])
        paper = self.run_it(doc)["layouts"][0]["paper"]
        self.assertEqual((paper["width_mm"], paper["height_mm"]), (279.4, 215.9))
        self.assertEqual(paper["paper_units"], "inches")

    def test_pixel_paper_has_no_millimetre_equivalent(self) -> None:
        doc = FakeDocument(layouts=[FakeLayout("Layout1", paper_units=2, paper=(1024.0, 768.0))])
        paper = self.run_it(doc)["layouts"][0]["paper"]
        self.assertIsNone(paper["width_mm"])
        self.assertEqual(paper["paper_units"], "pixels")

    def test_one_unconfigured_plotter_does_not_fail_the_listing(self) -> None:
        """GetPaperSize raises when the layout's plot device is missing."""
        doc = FakeDocument(
            layouts=[FakeLayout("Broken", tab_order=0, paper=None), FakeLayout("Layout1", tab_order=1)]
        )
        data = self.run_it(doc)
        self.assertEqual(len(data["layouts"]), 2)
        self.assertIsNone(data["layouts"][0]["paper"]["width_mm"])
        self.assertEqual(data["layouts"][1]["paper"]["width_mm"], 420.0)

    def test_a_zero_paper_size_is_an_absence_not_a_measurement(self) -> None:
        """An unavailable plot device does not always raise -- it can report 0 x 0.

        Observed live on the stock acadiso.dwt layouts, whose .pc3 path points at
        an AutoCAD 2005 install that does not exist on this machine. Passing the
        zero through would state the sheet is nought millimetres wide, which a
        caller reads as a measurement rather than as "size unknown".
        """
        doc = FakeDocument(
            layouts=[
                FakeLayout("NoPlotter", tab_order=0, paper_units=0, paper=(0.0, 0.0)),
                FakeLayout("Layout1", tab_order=1),
            ]
        )
        data = self.run_it(doc)
        paper = data["layouts"][0]["paper"]
        self.assertIsNone(paper["width_mm"])
        self.assertIsNone(paper["height_mm"])
        self.assertEqual(data["layouts"][1]["paper"]["width_mm"], 420.0)


class DrawToolFlaggingTests(unittest.TestCase):
    """Each drawing tool should ask get_space for exactly the method it calls."""

    CASES = {
        "draw_line": ({"start_mm": [0, 0], "end_mm": [1, 1]}, "AddLine"),
        "draw_circle": ({"center_mm": [0, 0], "radius_mm": 5}, "AddCircle"),
        "draw_polyline": ({"vertices_mm": [[0, 0], [1, 1]]}, "AddLightWeightPolyline"),
        "draw_text": ({"text": "x", "insertion_point_mm": [0, 0], "height_mm": 2}, "AddText"),
        "draw_arc": (
            {"center_mm": [0, 0], "radius_mm": 5, "start_angle_deg": 0, "end_angle_deg": 90},
            "AddArc",
        ),
        "draw_ellipse": ({"center_mm": [0, 0], "major_axis_mm": 5, "minor_axis_mm": 2}, "AddEllipse"),
        "draw_point": ({"point_mm": [0, 0]}, "AddPoint"),
        "insert_block": ({"block_name": "BOLT-M8", "insertion_point_mm": [0, 0]}, "InsertBlock"),
        "add_linear_dimension": (
            {"start_mm": [0, 0], "end_mm": [10, 0], "dimension_line_point_mm": [5, 5]},
            "AddDimAligned",
        ),
    }

    def test_exactly_one_method_is_flagged_per_call(self) -> None:
        for tool_name, (arguments, expected) in self.CASES.items():
            with self.subTest(tool=tool_name):
                run = run_tool(tool_name, arguments)
                self.assertEqual(run.space_calls, [(None, (expected,))])


if __name__ == "__main__":
    unittest.main()
