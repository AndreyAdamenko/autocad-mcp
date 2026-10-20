"""Inspect embedded COM data; draw QA plans through an anonymous Python pipe."""
import base64
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import zlib

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.geometry import bounds, world_geometry

raw = bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string()
lines = zlib.decompress(base64.b85decode(raw)).decode().splitlines()
manifest = json.loads(lines[0])['manifest']
records = [json.loads(line) for line in lines[1:] if line]
regions = json.loads((ROOT / 'evidence/ventilation/source-selection.json').read_text(encoding='utf-8'))['regions']
frames = {}
root_records = [r for r in records if r['id'].count('/') == 1]
for key in ('ground', 'upper', 'roof'):
    x0, x1, y0, y1 = regions[key]
    rows = [r for r in root_records if r.get('source_bbox') and
            x0 <= r['source_bbox'][0][0] <= r['source_bbox'][1][0] <= x1 and
            y0 <= r['source_bbox'][0][1] <= r['source_bbox'][1][1] <= y1]
    if key != 'roof':
        columns = [r for r in rows if r['geometry']['kind'] == 'block' and r.get('layer') == 'Несущие колонны']
        centres = [[sum(r['source_bbox'][j][i] for j in (0, 1)) / 2 for i in range(2)] for r in columns]
        origin = [min(p[i] for p in centres) for i in (0, 1)]
        spans = [max(p[i] for p in centres) - origin[i] for i in (0, 1)]
        evidence = [r['id'] for r in columns]
    else:
        vertical = [r for r in rows if r.get('layer') == 'Оси' and r['geometry']['kind'] == 'line' and
                    abs(r['geometry']['end'][0] - r['geometry']['start'][0]) < .01 and
                    abs(r['geometry']['end'][1] - r['geometry']['start'][1]) > 10000]
        horizontal = [r for r in rows if r.get('layer') == 'Оси' and r['geometry']['kind'] == 'line' and
                      abs(r['geometry']['end'][1] - r['geometry']['start'][1]) < .01 and
                      abs(r['geometry']['end'][0] - r['geometry']['start'][0]) > 20000]
        xs = [r['geometry']['start'][0] for r in vertical]
        ys = [r['geometry']['start'][1] for r in horizontal]
        origin, spans = [min(xs), min(ys)], [max(xs) - min(xs), max(ys) - min(ys)]
        evidence = [r['id'] for r in vertical + horizontal]
    if max(abs(a - b) for a, b in zip(spans, (30000, 12900))) > .25:
        raise ValueError('OV grid does not match building axes: ' + key)
    frames[key] = {'origin_mm': origin, 'spans_mm': spans, 'source_ids': evidence}

summaries = []
for r in root_records:
    g = r['geometry']
    if g['kind'] != 'block':
        continue
    children = [c for c in records if c['id'].startswith(r['id'] + '/')]
    summaries.append({'id': r['id'], 'layer': r.get('effective_layer'), 'geometry': g,
                      'bbox': r.get('source_bbox'), 'child_kinds': dict(Counter(c['geometry']['kind'] for c in children)),
                      'closed_profiles': [{'id': c['id'], 'vertices': len(c['geometry']['vertices']),
                                           'bbox': bounds(world_geometry(c)[0])}
                                          for c in children if c['geometry']['kind'] == 'polyline' and c['geometry'].get('closed')]})
result = {'source': manifest, 'frames': frames, 'record_count': len(records), 'blocks': summaries,
          'kind_counts': dict(Counter(r['geometry']['kind'] for r in records)),
          'issues': [{'id': r['id'], 'status': r['status'], 'reason': r['geometry'].get('reason')}
                     for r in records if r['status'] != 'ok']}
(ROOT / 'evidence/ventilation/com-source-analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
subprocess.run([r'C:\Python312\python.exe', '-X', 'utf8', '-B', str(ROOT / 'tools/render_ventilation_reference.py')],
               input=json.dumps({'records': records, 'frames': frames}, ensure_ascii=True).encode(), check=True)
print(json.dumps({'records': len(records), 'kinds': result['kind_counts'], 'frames': frames}, ensure_ascii=True))
