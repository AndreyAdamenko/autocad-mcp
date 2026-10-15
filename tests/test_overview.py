import unittest
from autocad_mcp.overview import section_intervals,window_gaps,gap_bands


def profile(identifier,points,category='Walls',layer='KG_Стены_Наружные'):
    return {'id':identifier,'points':points,'category':category,'layer':layer}


class OverviewTests(unittest.TestCase):
    def test_concave_wall_section_does_not_fill_its_notch(self):
        p=[[0,0],[3000,0],[3000,200],[1000,200],[1000,1000],[0,1000]]
        self.assertEqual(section_intervals(p,0,500),[(0,1000)])
        self.assertEqual(section_intervals(p,0,100),[(0,3000)])

    def test_rc_gasblock_alignment_and_float_boundaries_do_not_hide_a_real_window(self):
        rows=[profile('rc',[[0,-100+1e-9],[1000,-100+1e-9],[1000,100],[0,100]],'Unclassified construction','KG_Конструкции_50'),
              profile('gas',[[2050,-200],[3000,-200],[3000,0],[2050,0]]),
              profile('after_column',[[3400,-200],[4000,-200],[4000,0],[3400,0]])]
        gaps=window_gaps(rows,[30000,12900])
        self.assertEqual(len(gaps),1)
        self.assertEqual((gaps[0]['side'],gaps[0]['lo'],gaps[0]['hi']),('south',1000,2050))

    def test_flanking_physical_bands_preserve_40_mm_air_space(self):
        profiles=[]
        for lo,hi,cat in [(-200,0,'Walls'),(-300,-200,'Insulation'),(-460,-340,'Facade brick')]:
            for name,a,b in [('left',0,1000),('right',2050,3000)]:
                profiles.append(profile(name+cat,[[a,lo],[b,lo],[b,hi],[a,hi]],cat))
        gap={'axis':0,'lo':1000,'hi':2050,'wall_band':[-200,0],'section_coordinate_mm':-50}
        bands=gap_bands(profiles,gap)
        self.assertEqual(len(bands),3)
        self.assertEqual(sorted(tuple(b['perpendicular']) for b in bands),[(-460,-340),(-300,-200),(-200,0)])
        self.assertFalse(any(b['perpendicular']==[-340,-300] for b in bands))

    def test_one_flank_cannot_invent_a_matching_material(self):
        row=profile('single',[[0,-200],[1000,-200],[1000,0],[0,0]])
        self.assertEqual(gap_bands([row],{'axis':0,'lo':1000,'hi':2050,'wall_band':[-200,0]}),[])
