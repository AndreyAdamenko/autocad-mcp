"""Record the user's accepted overall-view scope, source selections and assumptions."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
project=json.loads((ROOT/'evidence/architectural_project_v3.json').read_text(encoding='utf-8'))
project.update(name='5800-1.3 overall exterior view',profile='overall_view',accepted_simplifications=True,
               base_model=str(ROOT/'artifacts/erdaut_architecture_final.blend'))
project['overview_sources']={key:str(ROOT/'artifacts'/file) for key,file in {
    'ground':'erdaut_checked.blend','upper':'erdaut_upper_plan.blend',
    'window':'spz_window_detail.blend','roof':'spz_roof_source.blend',
    'frame':'spz_frame_source.blend','stairs':'spz_stairs_source.blend',
    'parapet':'spz_parapet_detail.blend','section':'erdaut_section7.blend'}.items()}
mapping=json.loads((ROOT/'evidence/spz_staged_sources.json').read_text(encoding='utf-8'))
for item in mapping['files']:
    if any(s in item['relative'] for s in ('АР-6_','АР-8_9_REV.1','АР-13_','АР-14-17','АР-18-21','КЖ-009_','КЖ-024_')):
        project['evidence_drawings'].append({'path':item['source'],'sha256':item['sha256']})
project['overview_parameters']={
    'window_profile_depth_mm':{'value_mm':70,'confirmed':False,'status':'accepted secondary simplification'},
    'source_priority':'Current dimensioned REV3 window detail over stale plan block names/old facade heads',
    'scope':'Overall exterior appearance of the designed building, not construction detailing',
    'cornice_height_mm':{'value_mm':1220,'confirmed':True,'source_ids':['AR13:Model/475','AR13:Model/BAF']},
    'parapet_top_mm':{'value_mm':9670,'confirmed':True,'source_ids':['AR7:Model/2966','AR13:parapet node']},
    'window_product_mm':{'values':[1000,1750],'confirmed':True,'source_ids':['AR11_12:Model/19346']},
    'window_opening_mm':{'values':[1050,1800],'confirmed':True,'source_ids':['AR11_12:Model/1933A']},
    'roof_minimum_buildup_mm':{'value_mm':374,'confirmed':True,'source_ids':['AR7:Model/2618'],'derivation':'40+200+114+20, excluding thin membranes'},
    'colors':'Approximate RAL1001 brick / RAL7037 plinth, cornice and canopies / RAL7006 frames. Exact colorimetry and facet-by-facet accents not verified.'}
project['unresolved']=[
    {'part':'Overall-view scope','reason':'Interior stairs/railings, foundations, hidden joints, reinforcement and engineering systems are not detailed. Overall appearance only.'},
    {'part':'Secondary products','reason':'70 mm window frame depth, glazing mullions, closed steel door panels and canopy internal structures simplified; not confirmed product manufacturing geometry.'},
    {'part':'Roof and cornice','reason':'Roof slopes and thin membranes flattened to minimum buildup; source contour 9A3 interpreted as fascia envelope; 1220 mm height and +9.670 top confirmed.'},
    {'part':'Wall materials and joints','reason':'Unclassified source construction stays neutral. Concealed floor-edge cladding junctions simplified. Individual local wall/beam associations remain outside verified construction scope.'},
    {'part':'Facade pattern','reason':'Approximate specified colors applied; full gray accent-brick pattern and ventilation outlets not individually reconstructed.'},
    {'part':'Nominal steel doors','reason':'2080 mm height and nominal widths read from plan block designations; hinges, hardware and manufacturing clearances not modelled.'}]
(ROOT/'evidence/architectural_project_overview.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
print(ROOT/'evidence/architectural_project_overview.json')
