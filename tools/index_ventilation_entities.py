"""Read a bounded root-entity index via COM; no drawing edits or geometry export."""
import argparse
from collections import Counter
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pythoncom
import win32com.client.dynamic
from autocad_mcp.acad_core import com_apartment, running_app
from autocad_mcp.com_read import read, method, retry, open_source, maybe
from autocad_mcp.extraction import bbox, source_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    parser.error('Index output already exists')
library = pythoncom.LoadTypeLib(r'C:\Program Files\Common Files\Autodesk Shared\acax19enu.tlb')
properties = {}
for i in range(library.GetTypeInfoCount()):
    if library.GetDocumentation(i)[0] != 'IAcadEntity':
        continue
    info = library.GetTypeInfo(i)
    for k in range(info.GetTypeAttr().cFuncs):
        function = info.GetFuncDesc(k)
        if function.invkind == pythoncom.INVOKE_PROPERTYGET:
            properties[info.GetNames(function.memid)[0]] = function.memid


def scalar(raw, name):
    return retry(lambda: raw.InvokeTypes(properties[name], 0, pythoncom.DISPATCH_PROPERTYGET,
                                          (pythoncom.VT_BSTR, 0), ()))


with com_apartment():
    before = source_hash(args.source)
    doc = open_source(running_app(), args.source)
    initial = (int(method(doc, 'GetVariable', 'DBMOD')), int(read(read(doc, 'ModelSpace'), 'Count')))
    space = read(doc, 'ModelSpace')
    enum = space._NewEnum()._oleobj_
    rows = []
    layers = Counter()
    selected_layers = Counter()
    start = time.monotonic()
    count = 0
    checkpoint = args.output.with_suffix('.partial.json')
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved['sha256'] != before or tuple(saved['fingerprint']) != initial:
            raise ValueError('Partial index belongs to a different source state')
        rows, count = saved['rows'], saved['scanned']
        layers, selected_layers = Counter(saved['layers']), Counter(saved['selected_layers'])
        retry(lambda: enum.Skip(count))
        print('resuming', count, flush=True)
    while True:
        batch = retry(lambda: enum.Next(256))
        if not batch:
            break
        for raw in batch:
            count += 1
            layer = scalar(raw, 'Layer')
            layers[layer] += 1
            if not (layer.startswith(('ОВ_', 'KG_Вентиляция', 'm-')) or
                    layer in ('Несущие колонны', 'Оси', '_0', '0', 'Оборудование', 'kontury')):
                continue
            if layer in ('ОВ_Т1', 'ОВ_Т2', 'ОВ_R', 'ОВ_Изол', 'ОВ_Оси_Оборуд'):
                continue
            handle = scalar(raw, 'Handle')
            kind = scalar(raw, 'ObjectName')
            obj = win32com.client.dynamic.Dispatch(raw)
            row = {'handle': handle, 'type': kind, 'layer': layer, 'bbox': bbox(obj)}
            if kind in ('AcDbText', 'AcDbMText'):
                row['text'] = str(read(obj, 'TextString'))
                row['point'] = list(read(obj, 'InsertionPoint'))
            elif kind == 'AcDbMLeader':
                row['text'] = str(read(obj, 'TextString'))
                try:
                    row['leaders'] = [list(method(obj, 'GetLeaderLineVertices', i)) for i in range(int(read(obj, 'LeaderCount')))]
                except Exception as exc:
                    row['leader_read_error'] = str(exc)
            elif kind == 'AcDbBlockReference':
                row['name'] = str(read(obj, 'Name'))
                row['effective_name'] = str(maybe(obj, 'EffectiveName', row['name']))
                row['point'] = list(read(obj, 'InsertionPoint'))
            rows.append(row)
            selected_layers[layer] += 1
        print(json.dumps({'scanned': count, 'total': initial[1], 'selected': len(rows),
                          'seconds': round(time.monotonic() - start, 1)}), flush=True)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps({'sha256': before, 'fingerprint': initial, 'scanned': count,
                                          'rows': rows, 'layers': dict(layers), 'selected_layers': dict(selected_layers)},
                                         ensure_ascii=False), encoding='utf-8')
    after = source_hash(args.source)
    final = (int(method(doc, 'GetVariable', 'DBMOD')), int(read(read(doc, 'ModelSpace'), 'Count')))
    if before != after or initial != final or count != initial[1]:
        raise ValueError('Source changed or COM enumeration incomplete')
    report = {'source': str(args.source.resolve()), 'sha256': before, 'readonly': bool(read(doc, 'ReadOnly')),
              'root_count': count, 'layers': dict(layers), 'selected_layers': dict(selected_layers),
              'rows': rows, 'source_unchanged': True,
              'note': 'Selection/annotation analysis only. Mesh construction must reread selected handles through COM.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'index': str(args.output), 'rows': len(rows), 'seconds': time.monotonic() - start}), flush=True)
