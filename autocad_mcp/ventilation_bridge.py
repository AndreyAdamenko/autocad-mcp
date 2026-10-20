"""Launch the project-specific ventilation overview assembly through bpy."""
import json
import os
from pathlib import Path
import subprocess

from .blender_bridge import discover_blender


def build_ventilation(project_path, target, timeout=900):
    project_path = Path(project_path).resolve(strict=True)
    target = Path(target).resolve()
    if target.exists() or target.suffix.lower() != '.blend':
        raise ValueError('Output must be a new .blend')
    config = json.loads(project_path.read_text(encoding='utf-8'))
    if config.get('profile') != 'erdaut_ventilation_overview':
        raise ValueError('This assembly requires the explicit project-specific overview profile')
    for key in ('base_model', 'source_blend'):
        if Path(config[key]).resolve() == target:
            raise ValueError('Output cannot replace an input')
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path = target.with_suffix('.blender.log')
    worker = Path(__file__).with_name('ventilation_worker.py')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with log_path.open('w', encoding='utf-8') as log:
        completed = subprocess.run([str(discover_blender()), '--background', '--factory-startup', '--disable-autoexec',
                                    '--python-exit-code', '3', '--python', str(worker), '--', str(project_path), str(target)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, **options)
    report_path = target.with_suffix('.report.json')
    if completed.returncode or not target.is_file() or not report_path.is_file():
        raise RuntimeError('Ventilation assembly failed; see ' + str(log_path))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    vent = report['ventilation']
    return {'blend': str(target), 'report': str(report_path), 'log': str(log_path),
            'counts': vent['counts'], 'complete': vent['complete'],
            'issues': vent['issues'], 'verification': report['verification'], 'renders': vent.get('renders', [])}
