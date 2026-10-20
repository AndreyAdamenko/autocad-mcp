"""Read only layout annotations of OV sheets; compare common ModelSpace anchors."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.acad_core import com_apartment, running_app
from autocad_mcp.catalogue import inspect_document
from autocad_mcp.com_read import read, method, open_source
from autocad_mcp.extraction import source_hash, entity_geometry

folder = ROOT / 'evidence/spz_sources/5800_1.3_ОВ'
dest = ROOT / 'evidence/ventilation'
names = ['5800-1.3-ОВ-' + str(i).zfill(3) + '_REV.0.dwg' for i in (2, 3, 11, 12, 13, 14)]
names += ['5800-1.3-ОВ.СО_REV.1.dwg']
report_path = dest / 'layout-catalogues.json'
results = json.loads(report_path.read_text(encoding='utf-8'))['drawings'] if report_path.exists() else []
with com_apartment():
    app = running_app()
    for name in names:
        path = folder / name
        before = source_hash(path)
        previous = next((r for r in results if r['path'] == str(path)), None)
        if previous:
            if previous['sha256'] != before:
                raise ValueError('Previously read drawing changed')
            continue
        doc = open_source(app, path)
        report = inspect_document(doc, include_dimensions=False, scope='paper')
        report.update(sha256=before, source_unchanged=before == source_hash(path))
        if not report['source_unchanged']:
            raise ValueError('Source changed: ' + name)
        results.append(report)
        (dest / 'layout-catalogues.json').write_text(json.dumps({'drawings': results}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'file': name, 'texts': len(report['texts']), 'tables': len(report['tables']),
                          'titles': report['titles'][:4]}, ensure_ascii=False), flush=True)
    anchors = ['26BA30', '26BA2B', '26BA35', '273A07', '273A01', '2739FB',
               '27376D', '273767', '27CFCF', '27CFE4', '27CFDB', '27CFF2']
    checks = {}
    for number in (11, 12, 13, 14):
        doc = open_source(app, folder / f'5800-1.3-ОВ-{number:03}_REV.0.dwg')
        values = {}
        for handle in anchors:
            try:
                obj = method(doc, 'HandleToObject', handle)
                values[handle] = {'text': str(read(obj, 'TextString')), 'point': list(read(obj, 'InsertionPoint'))}
            except Exception as exc:
                values[handle] = {'error': str(exc)}
        checks[str(number)] = values
    (dest / 'common-anchor-comparison.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Common plan/scheme anchors match:', all(checks[str(n)] == checks['11'] for n in (12, 13, 14)), flush=True)
