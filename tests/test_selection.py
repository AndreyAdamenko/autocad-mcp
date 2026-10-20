"""Selection must not lose enclosing transforms or imply whole-DWG fidelity."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autocad_mcp.extraction import GeometryScan, entity_geometry, iter_records
from autocad_mcp.geometry import world_geometry
import math


class SourceSelectionTests(unittest.TestCase):
    def test_selection_options_without_roots_fail_before_com(self):
        from autocad_mcp.pipeline_tools import build_blender_model
        for options in ({'visible_only': True}, {'selection_evidence': 'Plan region'}):
            with self.subTest(options=options), patch('autocad_mcp.pipeline_tools.com_apartment') as com:
                with self.assertRaisesRegex(ValueError, 'require explicit root_handles'):
                    build_blender_model({'path': 'drawing.dwg', 'output': 'model.blend', **options})
                com.assert_not_called()

    def scan(self, owner=123):
        obj = SimpleNamespace(OwnerID=owner)
        doc = SimpleNamespace(ModelSpace=SimpleNamespace(ObjectID=123), HandleToObject=lambda _: obj)
        scan = GeometryScan.__new__(GeometryScan)
        scan.doc, scan.page, scan.pending = doc, 0, None
        scan.manifest = {'mm_per_unit': 1}
        return scan

    def test_block_children_are_not_accepted_as_untransformed_roots(self):
        with self.assertRaisesRegex(ValueError, 'not a ModelSpace root'):
            self.scan(999).select_root_handles(['A'], 'Proved drawing region')

    def test_duplicate_or_malformed_roots_are_rejected(self):
        for handles in [['a', 'A'], ['Model/A'], [], ['A', 1]]:
            with self.subTest(handles=handles), self.assertRaises(ValueError):
                self.scan().select_root_handles(handles, 'selection')

    def test_explicit_selection_retains_origin_and_scope(self):
        scan = self.scan()
        with patch('autocad_mcp.extraction.iter_records', return_value=iter([])) as records:
            scan.select_root_handles(['A'], 'Selected actual ventilation plan')
        selection = scan.manifest['selection']
        self.assertFalse(selection['whole_drawing'])
        self.assertEqual(selection['root_handles'], ['A'])
        self.assertEqual(records.call_args.args[1][0][1].Count, 1)

    def test_consumed_scan_or_unexplained_selection_is_rejected(self):
        scan = self.scan()
        scan.page = 1
        with self.assertRaises(ValueError):
            scan.select_root_handles(['A'], 'selection')
        with self.assertRaises(ValueError):
            self.scan().select_root_handles(['A'], '')

    def test_mleader_keeps_text_and_arrow_vertices_without_claiming_graphics(self):
        def leader_index(index):
            if index != 3:
                raise ValueError('Deleted or absent leader line')
            return 7
        obj = SimpleNamespace(TextString='400x200', LeaderCount=1, GetLeaderIndex=leader_index,
                              GetLeaderLineIndexes=lambda _: [3],
                              GetLeaderLineVertices=lambda _: (100, 200, 0, 300, 400, 0))
        geometry = entity_geometry(obj, 'AcDbMLeader')
        self.assertEqual(geometry['kind'], 'unsupported')
        self.assertEqual(geometry['text'], '400x200')
        self.assertEqual(geometry['leader_lines'][0]['vertices'][0], [100, 200, 0])

    def test_mline_center_path_does_not_claim_complete_style_geometry(self):
        obj = SimpleNamespace(Coordinates=(0, 0, 0, 100, 200, 0), StyleName='DUCT',
                              MLineScale=100, Justification=1)
        geometry = entity_geometry(obj, 'AcDbMline')
        self.assertEqual(geometry['kind'], 'unsupported')
        self.assertEqual(geometry['centerline'], [[0, 0, 0], [100, 200, 0]])

    def test_definition_cache_preserves_each_instance_transform_and_layer(self):
        leaf = SimpleNamespace(Handle='C', ObjectName='AcDbLine', Layer='0', Visible=True,
                               StartPoint=[0, 0, 0], EndPoint=[10, 0, 0], Length=10,
                               GetBoundingBox=lambda: ([0, 0, 0], [10, 0, 0]))
        block = SimpleNamespace(Count=1, IsXRef=False, Origin=[0, 0, 0], Units=4,
                                Item=lambda _: leaf)

        def reference(handle, layer, point, rotation=0, xscale=1):
            return SimpleNamespace(Handle=handle, ObjectName='AcDbBlockReference', Layer=layer, Visible=True,
                                   Name='DEF', Normal=[0, 0, 1], InsertionPoint=point, Rotation=rotation,
                                   XScaleFactor=xscale, YScaleFactor=1, ZScaleFactor=1)

        roots = [reference('A', 'Supply', [100, 0, 0]), reference('B', 'Extract', [0, 200, 0], math.pi / 2, -1)]
        doc = SimpleNamespace(Blocks=SimpleNamespace(Item=lambda _: block))
        space = SimpleNamespace(Count=2, Item=lambda i: roots[i])
        with patch('autocad_mcp.extraction.entity_geometry', wraps=entity_geometry) as geometry:
            records = list(iter_records(doc, [('Model', space)], 1))
        children = [r for r in records if r['type'] == 'AcDbLine']
        self.assertEqual(geometry.call_count, 1)
        self.assertEqual([r['id'] for r in children], ['Model/A/C', 'Model/B/C'])
        self.assertEqual([r['effective_layer'] for r in children], ['Supply', 'Extract'])
        self.assertAlmostEqual(world_geometry(children[0])[0][1][0], 110)
        self.assertAlmostEqual(world_geometry(children[1])[0][1][1], 190)

    def test_xref_children_are_not_enumerated_by_the_cache(self):
        from unittest.mock import Mock
        children = Mock(side_effect=AssertionError('Unverified xref must not be expanded'))
        block = SimpleNamespace(Count=1000, IsXRef=True, Origin=[0, 0, 0], Units=4, Item=children)
        root = SimpleNamespace(Handle='A', ObjectName='AcDbBlockReference', Layer='0', Name='XREF',
                               Normal=[0, 0, 1], InsertionPoint=[0, 0, 0], Rotation=0,
                               XScaleFactor=1, YScaleFactor=1, ZScaleFactor=1)
        doc = SimpleNamespace(Blocks=SimpleNamespace(Item=lambda _: block))
        records = list(iter_records(doc, [('Model', SimpleNamespace(Count=1, Item=lambda _: root))], 1))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'unsupported')
        children.assert_not_called()


if __name__ == '__main__':
    unittest.main()
