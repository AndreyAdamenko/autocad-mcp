import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autocad_mcp.architecture import closed_footprint,in_frame,local_footprint,orthogonal_slab,plan_frame,rectangular_slab,validate_project
from autocad_mcp.geometry import identity
from autocad_mcp.com_read import read,ready_document


class ArchitecturalTests(unittest.TestCase):
    def test_required_entity_metadata_can_wait_but_unknown_optional_member_cannot(self):
        calls=[]
        class Entity:
            @property
            def ObjectName(self):
                calls.append(1)
                if len(calls)==1:raise AttributeError('<unknown>.ObjectName')
                return 'AcDbLine'
        with patch('autocad_mcp.com_read.time.sleep'),patch('autocad_mcp.com_read.pythoncom.PumpWaitingMessages'):
            self.assertEqual(read(Entity(),'ObjectName'),'AcDbLine')
            with self.assertRaises(AttributeError):read(Entity(),'NotIn2014')
        self.assertEqual(len(calls),2)

    def test_nested_bounds_are_selected_in_world_coordinates(self):
        matrix=identity()
        matrix[0][3],matrix[1][3]=50000,25000
        record={'source_bbox':[[0,0,0],[1000,200,0]],'world_matrix':matrix}
        self.assertTrue(in_frame(record,{'origin_mm':[50000,25000],'axis_spans_mm':[30000,12900]}))
        self.assertFalse(in_frame(record,{'origin_mm':[0,0],'axis_spans_mm':[30000,12900]}))

    def test_slab_opening_removes_correct_volume_and_leaves_closed_mesh(self):
        vertices,faces,volume=rectangular_slab([[0,0],[4000,3000]],[[[1000,1000],[2000,2000]]],3750,200)
        self.assertEqual(volume,2200000000)
        from collections import Counter
        from autocad_mcp.geometry import cross
        edges=Counter()
        signed=0
        for f in faces:
            for i in range(len(f)):
                edges[tuple(sorted((f[i],f[(i+1)%len(f)])))]+=1
            for i in range(1,len(f)-1):
                a,b,c=[vertices[k] for k in (f[0],f[i],f[i+1])]
                signed+=sum(x*y for x,y in zip(a,cross(b,c)))/6
        self.assertTrue(all(n==2 for n in edges.values()))
        self.assertAlmostEqual(abs(signed),volume,places=3)

    def test_slab_cannot_silently_accept_openings_outside_its_outline(self):
        with self.assertRaisesRegex(ValueError,'strictly inside'):
            rectangular_slab([[0,0],[4000,3000]],[[[-100,100],[100,200]]],0,200)
    def records(self):
        result=[{'id':'Model/TITLE','world_matrix':identity(),'geometry':{'kind':'text','text':'План','point':[15000,17000,0]}}]
        for x in [0,3000,6000,12000,18000,24000,27000,30000]:
            for y in [0,6300,12900]:
                result.append({'id':f'Model/C{x}_{y}','space':'Model','effective_layer':'Несущие колонны',
                               'geometry':{'kind':'block','effective_name':'Колонна 400x400'},
                               'source_bbox':[[x-200,y-200,0],[x+200,y+200,0]]})
        return result

    def test_translated_floor_grid_is_recovered_from_source_columns(self):
        records=self.records()
        for record in records[1:]:
            for p in record['source_bbox']:
                p[0]+=900
                p[1]+=20
        frame=plan_frame(records,'Model/TITLE')
        self.assertEqual(frame['origin_mm'],[900,20])
        self.assertEqual(frame['axis_spans_mm'],[30000,12900])
        self.assertEqual(len(frame['grid_mm']),24)

    def test_wrong_column_dimensions_or_missing_column_are_rejected(self):
        records=self.records()
        records.pop()
        with self.assertRaisesRegex(ValueError,'24 source columns'):
            plan_frame(records,'Model/TITLE')
        records=self.records()
        records[1]['source_bbox'][1][0]+=10
        with self.assertRaisesRegex(ValueError,'400x400'):
            plan_frame(records,'Model/TITLE')

    def test_unknown_height_cannot_become_confirmed(self):
        with self.assertRaisesRegex(ValueError,'confirmed drawing evidence'):
            validate_project({'floors':[{'base':{'value_mm':0,'confirmed':False}}]})

    def test_slab_height_also_requires_confirmed_drawing_evidence(self):
        def parameter(value):
            return {'value_mm':value,'confirmed':True,'source_ids':['drawing/handle']}
        project={'floors':[{'base':parameter(0),'wall_top':parameter(3750)}],
                 'column_top':parameter(7800),'evidence_drawings':[{'path':'source.dwg'}],
                 'slabs':[{'top':parameter(3950),'thickness':{'value_mm':200,'confirmed':False}}]}
        with self.assertRaisesRegex(ValueError,'confirmed drawing evidence'):
            validate_project(project)

    def test_sloping_source_cannot_silently_become_horizontal_wall(self):
        with patch('autocad_mcp.architecture.world_geometry',return_value=(
                [[0,0,0],[1000,0,0],[1000,1000,100],[0,1000,0]],[],[],True)):
            with self.assertRaisesRegex(ValueError,'must be horizontal'):
                local_footprint({}, {'origin_mm':[0,0]})

    def test_repeated_endpoint_proves_closure_without_autocad_closed_flag(self):
        polygon=[[0,0,0],[2000,0,0],[2000,2000,0],[0,2000,0],[0,0,0]]
        with patch('autocad_mcp.architecture.world_geometry',return_value=(polygon,[],[],False)):
            self.assertEqual(len(closed_footprint({})),4)
        polygon[-1]=[0,0.01,0]
        with patch('autocad_mcp.architecture.world_geometry',return_value=(polygon,[],[],False)):
            with self.assertRaisesRegex(ValueError,'contour is open'):
                closed_footprint({})

    def test_notched_opening_retains_source_notch_volume(self):
        polygon=[[1000,1000],[2000,1000],[2000,1900],[1900,1900],[1900,2000],[1000,2000]]
        vertices,faces,volume=orthogonal_slab([[0,0],[4000,3000]],[polygon],3750,200)
        self.assertEqual(volume,(12000000-990000)*200)
        self.assertIn([1900,1900,3750],vertices)
        from collections import Counter
        edges=Counter(tuple(sorted((f[i],f[(i+1)%len(f)]))) for f in faces for i in range(len(f)))
        self.assertTrue(all(n==2 for n in edges.values()))

    def test_open_document_waits_for_required_collection_type_information(self):
        calls=[]
        class Delayed:
            @property
            def Count(self):
                calls.append(1)
                if len(calls)==1:
                    raise AttributeError('<unknown>.Count')
                return 0
        doc=SimpleNamespace(FullName='test.dwg',Layouts=Delayed(),ModelSpace=SimpleNamespace(Count=0))
        with patch('autocad_mcp.com_read.time.sleep'),patch('autocad_mcp.com_read.pythoncom.PumpWaitingMessages'):
            self.assertIs(ready_document(doc),doc)
        self.assertEqual(len(calls),2)
