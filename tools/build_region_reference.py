"""Stream a selected region of a byte-identical staged DWG via COM into final BLEND."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from autocad_mcp.acad_core import com_apartment,running_app
from autocad_mcp.com_read import method,open_source,read
from autocad_mcp.extraction import bbox,GeometryScan,iter_records,source_hash
from autocad_mcp.blender_bridge import build

class ReadSelection:
    def __init__(self,objects):self.objects=objects;self.Count=len(objects)
    def Item(self,index):return self.objects[index]
    def _FlagAsMethod(self,name):pass

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--source',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--bbox',type=float,nargs=4)
args=p.parse_args()
source=args.source.resolve(strict=True)
if not source.is_relative_to(ROOT/'evidence/spz_sources'):
    raise ValueError('Only a staged local DWG may be opened by this tool')
mapping=json.loads((ROOT/'evidence/spz_staged_sources.json').read_text(encoding='utf-8'))
origin=next(x for x in mapping['files'] if Path(x['staged']).resolve()==source)
if source_hash(origin['source'])!=origin['sha256'] or source_hash(source)!=origin['sha256']:
    raise ValueError('Original and staged DWG must still match')
with com_apartment():
    doc=open_source(running_app(),source)
    scan=GeometryScan(doc,'model')
    scan.manifest.update(origin_source_path=origin['source'],origin_source_sha256=origin['sha256'],
                         byte_identical_staged_copy=True)
    if args.bbox:
        model=read(doc,'ModelSpace');objects=[]
        for i in range(int(read(model,'Count'))):
            obj=method(model,'Item',i)
            b=bbox(obj)
            if b and all(b[1][axis]>=args.bbox[axis] and b[0][axis]<=args.bbox[axis+2] for axis in (0,1)):
                objects.append(obj)
        scan.manifest['region_selection']={'bbox_mm':args.bbox,'selected_roots':len(objects),'model_roots':int(read(model,'Count'))}
        scan.spaces=[('Model',ReadSelection(objects))]
        scan.baseline=scan.fingerprint()
        scan.iterator=iter_records(doc,scan.spaces,scan.manifest['mm_per_unit'])
        print(json.dumps(scan.manifest['region_selection']),flush=True)
    result=build(scan,args.output)
if source_hash(origin['source'])!=origin['sha256']:
    raise ValueError('Original DWG changed during read')
print(json.dumps(result,ensure_ascii=False,indent=2))
