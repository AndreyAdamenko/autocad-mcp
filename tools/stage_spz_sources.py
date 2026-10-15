"""Stage byte-identical DWGs locally so AutoCAD cannot create source-folder locks."""
import hashlib
import json
import shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

inventory=json.loads((ROOT/'evidence/spz_inventory_before.json').read_text(encoding='utf-8-sig'))
source_root=Path(inventory['source'])
cache_root=ROOT/'evidence/spz_sources'
entries=[]
for item in inventory['files']:
    source=Path(item['FullName'])
    if source.suffix.lower() not in ('.dwg','.jpeg','.docx','.txt'):
        continue
    relative=source.relative_to(source_root)
    target=cache_root/relative
    before=digest(source)
    if target.exists() and digest(target)!=before:
        raise ValueError('Existing staged source differs; choose a fresh cache directory: '+str(target))
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
    if digest(target)!=before or digest(source)!=before:
        raise ValueError('Source/copy hash changed: '+str(source))
    entries.append({'relative':str(relative),'source':str(source),'staged':str(target),'sha256':before,
                    'source_unchanged':True,'byte_identical':True})
(ROOT/'evidence/spz_staged_sources.json').write_text(json.dumps({'source_root':str(source_root),'cache_root':str(cache_root),
    'copy_method':'byte-identical DWG; no conversion; source folder never opened for writing','files':entries},
    ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'staged':len(entries),'dwg':sum(Path(x['source']).suffix.lower()=='.dwg' for x in entries)},ensure_ascii=False))
