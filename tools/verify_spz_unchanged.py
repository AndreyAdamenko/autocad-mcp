"""Read-only comparison of the protected network folder against its initial inventory."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
before=json.loads((ROOT/'evidence/spz_inventory_before.json').read_text(encoding='utf-8-sig'))
mapping=json.loads((ROOT/'evidence/spz_staged_sources.json').read_text(encoding='utf-8'))
source=Path(before['source']);previous={str(Path(f['FullName'])).lower():f for f in before['files']}
current={str(p).lower():p for p in source.rglob('*') if p.is_file()}
added=sorted(set(current)-set(previous));removed=sorted(set(previous)-set(current));changed=[]
for key in set(previous)&set(current):
    old=previous[key];stat=current[key].stat()
    # Initial PowerShell timestamps carry 100 ns precision; Python's datetime
    # comparison is at microseconds. SHA-256 independently covers every read DWG.
    old_time=datetime.fromisoformat(old['LastWriteTimeUtc'].replace('Z','+00:00'))
    new_time=datetime.fromtimestamp(stat.st_mtime,timezone.utc)
    if stat.st_size!=old['Length'] or abs((new_time-old_time).total_seconds())>0.000001:
        changed.append({'path':str(current[key]),'size_before':old['Length'],'size_after':stat.st_size,
                        'mtime_before':old['LastWriteTimeUtc'],'mtime_after':new_time.isoformat()})
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()
hash_checks=[{'path':f['source'],'sha256_before':f['sha256'],'sha256_after':digest(f['source'])} for f in mapping['files']]
for item in hash_checks:item['unchanged']=item['sha256_before']==item['sha256_after']
report={'source':str(source),'files_before':len(previous),'files_after':len(current),'added':added,'removed':removed,
        'metadata_changed':changed,'hash_checks':hash_checks,'hashes_checked':len(hash_checks),
        'passed':not (added or removed or changed) and all(f['unchanged'] for f in hash_checks),
        'note':'Read access time is not compared. Existing AutoCAD lock files are included; none created, removed or rewritten by this workflow.'}
(ROOT/'evidence/spz_unchanged_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='hash_checks'},ensure_ascii=True))
if not report['passed']:raise SystemExit(1)
