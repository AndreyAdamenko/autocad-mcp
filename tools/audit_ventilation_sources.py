"""Record/compare protected source files while ventilation is added locally."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'evidence/ventilation'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def inventory(folder):
    result = {}
    for path in folder.rglob('*'):
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(folder))] = {'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    mapping = json.loads((ROOT / 'evidence/spz_staged_sources.json').read_text(encoding='utf-8'))
    folder = Path(mapping['source_root'])
    before_path = DEST / 'source-preservation-before.json'
    DEST.mkdir(exist_ok=True)
    if not args.verify:
        if before_path.exists():
            parser.error('Baseline already exists; it must not be overwritten')
        files = json.loads((DEST / 'sources.json').read_text(encoding='utf-8'))
        for item in files:
            if digest(item['source']) != item['sha256'] or digest(item['staged']) != item['sha256']:
                raise ValueError('Original/copy differs from previously read source: ' + item['source'])
        report = {'source_root': str(folder), 'inventory': inventory(folder), 'files': files,
                  'base_blend': str(ROOT / 'artifacts/erdaut_overall_view.blend'),
                  'base_blend_sha256': digest(ROOT / 'artifacts/erdaut_overall_view.blend')}
        before_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'baseline': str(before_path), 'files': len(report['inventory']),
                          'dwg_hashes': len(files)}, ensure_ascii=True))
    else:
        before = json.loads(before_path.read_text(encoding='utf-8'))
        current = inventory(folder)
        old = before['inventory']
        added = sorted(set(current) - set(old))
        removed = sorted(set(old) - set(current))
        modified = [key for key in old.keys() & current.keys() if old[key] != current[key]]
        checks = [{'source': f['source'], 'original_unchanged': digest(f['source']) == f['sha256'],
                   'copy_unchanged': digest(f['staged']) == f['sha256']} for f in before['files']]
        base_ok = digest(before['base_blend']) == before['base_blend_sha256']
        passed = not (added or removed or modified) and base_ok and all(
            c['original_unchanged'] and c['copy_unchanged'] for c in checks)
        report = {'passed': passed, 'files_before': len(old), 'files_after': len(current),
                  'added': added, 'removed': removed, 'modified': modified, 'checks': checks,
                  'base_blend_unchanged': base_ok, 'access_times_compared': False}
        (DEST / 'source-preservation-after.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in report.items() if key != 'checks'}, ensure_ascii=True))
        if not passed:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
