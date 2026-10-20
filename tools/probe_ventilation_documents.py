"""Read minimal COM metadata for staged OV documents without activating them."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.acad_core import com_apartment, running_app
from autocad_mcp.com_read import method, read, maybe

with com_apartment():
    app = running_app()
    docs = read(app, 'Documents')
    print('documents', int(read(docs, 'Count')), flush=True)
    for i in range(int(read(docs, 'Count'))):
        doc = method(docs, 'Item', i)
        path = str(read(doc, 'FullName'))
        if 'spz_sources' not in path or '_ОВ' not in path:
            continue
        print(json.dumps({'path': path, 'readonly': bool(read(doc, 'ReadOnly')),
                          'model_entities': int(read(read(doc, 'ModelSpace'), 'Count'))}, ensure_ascii=False), flush=True)
        layouts = read(doc, 'Layouts')
        for j in range(int(read(layouts, 'Count'))):
            layout = method(layouts, 'Item', j)
            name = str(read(layout, 'Name'))
            if name == 'Model':
                continue
            block = read(layout, 'Block')
            count = int(read(block, 'Count'))
            print('layout', name, count, flush=True)
            if count > 500:
                continue
            for k in range(count):
                obj = method(block, 'Item', k)
                if str(read(obj, 'ObjectName')) == 'AcDbViewport':
                    row = {'layout': name, 'id': str(read(obj, 'Handle'))}
                    row.update({key: maybe(obj, key) for key in (
                        'Target', 'Direction', 'Center', 'Width', 'Height', 'CustomScale',
                        'TwistAngle', 'ViewportOn')})
                    print(json.dumps(row, ensure_ascii=False, default=list), flush=True)
