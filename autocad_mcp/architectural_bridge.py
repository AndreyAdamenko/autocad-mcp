"""Launch the drawing-backed architectural assembly from MCP/CLI."""
import json
import os
import subprocess
from pathlib import Path

from .architecture import validate_project
from .blender_bridge import discover_blender


def build_architecture(project_path,target,timeout=600):
    project_path=Path(project_path).resolve(strict=True)
    project=validate_project(json.loads(project_path.read_text(encoding='utf-8')))
    target=Path(target).resolve()
    if target.exists() or target.suffix.lower()!='.blend':
        raise ValueError('Output must be a new .blend')
    target.parent.mkdir(parents=True,exist_ok=True)
    log_path=target.with_suffix('.blender.log')
    worker=Path(__file__).with_name('overview_worker.py' if project.get('profile')=='overall_view' else 'architectural_worker.py')
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    with log_path.open('w',encoding='utf-8') as log:
        process=subprocess.run([str(discover_blender()),'--background','--factory-startup','--disable-autoexec',
                                '--python-exit-code','3','--python',str(worker),'--',str(project_path),str(target)],
                               stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,timeout=timeout,**options)
    report_path=target.with_suffix('.report.json')
    if process.returncode or not target.is_file() or not report_path.is_file():
        raise RuntimeError(f'Architectural assembly failed; see {log_path}')
    report=json.loads(report_path.read_text(encoding='utf-8'))
    return {'blend':str(target),'report':str(report_path),'log':str(log_path),'counts':report['counts'],
            'verification':report['verification'],'renders':report['renders'],'unresolved':report['unresolved']}
