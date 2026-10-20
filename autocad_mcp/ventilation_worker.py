"""Add drawing-backed ventilation envelopes to the preserved building model."""
import base64
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import zlib

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.architectural_worker import checks_for, digest
from autocad_mcp.blender_worker import collection, mesh_object, extrude
from autocad_mcp.geometry import bounds, transform, world_geometry
from autocad_mcp.ventilation import insulation_sweep, plain_text, section_from_text, sweep
from autocad_mcp.ventilation_details import build_details
from autocad_mcp.ventilation_project import (block_body, callouts, column_frames, inferred_round_system,
                                            inside_plan, local_point, property_value, section_for_body,
                                            visible_descendants)


def unpack(name):
    raw = ''.join(bpy.data.texts[name + '.zlib.base85'].as_string().split())
    return zlib.decompress(base64.b85decode(raw)).decode()


def pack(name, data):
    name += '.zlib.base85'
    if name in bpy.data.texts:
        bpy.data.texts.remove(bpy.data.texts[name])
    encoded = base64.b85encode(zlib.compress(json.dumps(data, ensure_ascii=True).encode())).decode()
    bpy.data.texts.new(name).write('\n'.join(encoded[i:i + 120] for i in range(0, len(encoded), 120)))


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    config = json.loads(Path(args[0]).read_text(encoding='utf-8'))
    if config.get('profile') != 'erdaut_ventilation_overview' or config.get('accepted_overview_assumptions') is not True:
        raise ValueError('Explicit project-specific overview profile with accepted assumptions required')
    target = Path(args[1]).resolve()
    if target.exists():
        raise ValueError('New BLEND name required')
    base = Path(config['base_model']).resolve(strict=True)
    base_hash = digest(base)
    bpy.ops.wm.open_mainfile(filepath=config['source_blend'], load_ui=False, use_scripts=False)
    lines = unpack('AutoCAD source provenance.jsonl').splitlines()
    manifest = json.loads(lines[0])['manifest']
    records = [json.loads(line) for line in lines[1:] if line]
    if not manifest.get('readonly') or manifest.get('mm_per_unit') != 1:
        raise ValueError('Verified read-only millimetre COM provenance required')
    if digest(manifest['source_path']) != manifest['source_sha256']:
        raise ValueError('Ventilation DWG changed after COM capture')
    for source in config.get('supplementary_sources', []):
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        extra = unpack('AutoCAD source provenance.jsonl').splitlines()
        header = json.loads(extra[0])['manifest']
        if not header.get('readonly') or header['source_sha256'] != manifest['source_sha256'] or header['mm_per_unit'] != 1:
            raise ValueError('Supplementary records must belong to the same unchanged read-only DWG')
        known = {r['id'] for r in records}
        records.extend(json.loads(line) for line in extra[1:] if line and json.loads(line)['id'] not in known)
    by_id = {r['id']: r for r in records}
    frames = column_frames(records, config['regions'])
    catalogues = json.loads(Path(config['catalogue_path']).read_text(encoding='utf-8'))
    for drawing in catalogues['drawings']:
        if not drawing.get('readonly') or not drawing.get('source_unchanged') or digest(drawing['path']) != drawing['sha256']:
            raise ValueError('Specification/notes provenance changed: ' + drawing['path'])
    specification = next(d for d in catalogues['drawings'] if 'ОВ.СО_REV.1' in d['path'])
    ahu = next(row for table in specification['tables'] for row in table['rows'] if 'VVS021' in ' '.join(row))
    size_match=re.search(r'(0[.,]\d+)х(0[.,]\d+)\(h\)х(\d+[.,]\d+)м', ' '.join(ahu))
    if not size_match or [round(float(s.replace(',','.'))*1000) for s in size_match.groups()] != [961,986,3378]:
        raise ValueError('Latest specification no longer supports the selected AHU dimensions')
    levels = {
        ('П1', 'ground'): (3150, 'Model/27CFE4'), ('П1', 'upper'): (7250, 'Model/27CFCF'),
        ('В1', 'ground'): (3100, 'Model/27CFDB'), ('В1', 'upper'): (7200, 'Model/27CFD5'),
        ('В2', 'ground'): (3185, 'Model/27394B'),
        ('В3', 'ground'): (3500, 'Model/273A07'), ('В3', 'upper'): (7550, 'Model/273A01'),
        ('В4', 'ground'): (3500, 'Model/273767'), ('В4', 'upper'): (7550, 'Model/27376D'),
        ('В5', 'ground'): (3300, 'Model/27EF5F'),
    }
    for value, source in levels.values():
        text = plain_text(by_id[source]['geometry']['text']).replace(',', '.')
        if abs(float(re.search(r'[+-]?\d+\.\d+', text).group()) * 1000 - value) > .001:
            raise ValueError('Level rule contradicts source annotation: ' + source)
    bpy.ops.wm.open_mainfile(filepath=str(base), load_ui=False, use_scripts=False)
    audit = json.loads(unpack('Architectural reconstruction audit.json'))
    combined = json.loads(unpack('AutoCAD combined source provenance.json'))
    combined['ventilation'] = {'manifest': manifest, 'records': records, 'source_blend': config['source_blend']}
    combined['ventilation_catalogues'] = catalogues
    model = collection('Engineering - Ventilation')
    objects, added, issues, excluded, assumptions = [], [], [], [], []
    colors = {'Ventilation ducts': (.25, .56, .75, 1), 'Ventilation fittings': (.31, .62, .74, 1),
              'Ventilation terminals': (.8, .84, .85, 1), 'Ventilation equipment': (.27, .43, .58, 1),
              'Ventilation insulation': (.65, .76, .79, 1)}
    parents = {}
    scratch = collection('Construction scratch', model)

    def add(vertices, faces, identifier, category, system, floor, source_ids, section,
            note, status='derived', rule=None):
        element_id = 'Ventilation/' + identifier
        if (system, floor) not in parents:
            parents[(system, floor)] = collection(system + ' - ' + floor, model)
        parent = parents[(system, floor)]
        obj = mesh_object(element_id, vertices, [], faces, parent)
        box = bounds(vertices)
        obj['element_id'], obj['source_id'] = element_id, ';'.join(source_ids)
        obj['category'], obj['system'] = category, system
        obj['height_mm'], obj['base_z_mm'] = box[1][2] - box[0][2], box[0][2]
        obj['section_label'] = ('Ø' + str(round(section['diameter_mm'], 3)) + ' мм' if section['shape'] == 'round'
                                else f"{section['width_mm']:g} × {section['height_mm']:g} мм")
        obj['evidence_status'], obj['interpretation'] = status, note
        color=colors[category]
        if category in ('Ventilation ducts','Ventilation fittings','Ventilation terminals'):
            color=(.18,.48,.8,1) if system=='П1' else (.83,.28,.18,1) if system=='В1' else (.18,.64,.42,1) if system.startswith('ВЕ') else (.92,.57,.15,1)
        obj.color = color
        objects.append((obj, vertices))
        added.append({'element_id': element_id, 'source_id': ';'.join(source_ids), 'source_ids': source_ids,
                      'category': category, 'system': system, 'floor': floor, 'expected_vertices_mm': vertices,
                      'base_mm': box[0][2], 'top_mm': box[1][2], 'section': section,
                      'evidence_status': status, 'interpretation': note, 'ventilation_rule': rule or {}})
        return obj

    def add_prism(polygon, base_z, height, identifier, category, system, floor, source_ids, section, note, **kw):
        # Use the existing tested polygon extruder, then register the same mesh.
        obj, expected = extrude([[p[0], p[1], 0] for p in polygon],
                                {'height_mm': {'value': height}, 'base_z_mm': {'value': base_z}, 'category': category},
                                scratch, identifier, .25)
        faces = [list(face.vertices) for face in obj.data.polygons]
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.meshes.remove(data)
        return add(expected, faces, identifier, category, system, floor, source_ids, section, note, **kw)

    for floor in ('ground', 'upper'):
        frame = frames[floor]
        labels = callouts(records, frame, duct_only=True)
        roots = [r for r in records if r['id'].count('/') == 1 and r.get('visible') is not False
                 and r['geometry']['kind'] == 'block' and inside_plan(r, frame)]
        for root in roots:
            name = root['geometry'].get('effective_name', '').lower()
            layer = root.get('effective_layer', '')
            try:
                if name in ('hvac. duct 1', 'duct приток'):
                    body = block_body(records, root, frame)
                    section, label, distance = section_for_body(body, labels)
                    system = 'П1' if 'Прит' in layer else 'В1'
                    z, source = levels[(system, floor)]
                    if system == 'П1' and floor == 'ground' and section['height_mm'] >= 300:
                        z, source = 3050, 'Model/27CFF2'
                    add_prism(body['polygon'], z, section['height_mm'], floor + '/' + root['handle'],
                              'Ventilation ducts', system, floor, [root['id'], *body['source_ids'], label, source],
                              section, 'Measured plan body edges; height associated heuristically with nearest same-width callout, not independently confirmed. Rectangular elevation interpreted as underside.',
                              status='accepted_assumption',
                              rule={'kind': 'rectangular_body', 'root': root['id'], 'height_source': label,
                                    'level_source': source, 'label_distance_mm': distance,
                                    'path_mm': [p[:2]+[z+section['height_mm']/2] for p in body['path']]})
                    if system == 'П1':
                        path=[p[:2]+[z+section['height_mm']/2] for p in body['path']]
                        vertices,faces=insulation_sweep(path,section,6)
                        add(vertices,faces,floor+'/'+root['handle']+'/insulation','Ventilation insulation',system,floor,
                            [root['id'],'OV002:1/12F2'],section,'6 mm supply insulation from general notes; closed annular shell.',
                            rule={'kind':'insulation','thickness_mm':6,'path_mm':path})
                elif name == 'возд круглый':
                    body = block_body(records, root, frame, round_section=True)
                    center = [sum(p[i] for p in body['path']) / 2 for i in range(3)]
                    system = inferred_round_system(center)
                    if (system, floor) not in levels:
                        excluded.append({'id': root['id'], 'reason': 'No horizontal branch elevation on this floor; inspect vertical continuation'})
                        continue
                    z, source = levels[(system, floor)]
                    path = [[p[0], p[1], z] for p in body['path']]
                    vertices, faces = sweep(path, body['section'])
                    add(vertices, faces, floor + '/' + root['handle'], 'Ventilation ducts', system, floor,
                        [root['id'], *body['source_ids'], source], body['section'],
                        'Visible parallel duct edges and DN visibility; round elevation is centre axis. System selected by the labelled plan cluster.',
                        rule={'kind': 'round_body', 'root': root['id'], 'level_source': source,'path_mm':path})
            except (ValueError, KeyError) as exc:
                if name in ('hvac. duct 1', 'duct приток', 'возд круглый'):
                    issues.append({'id': root['id'], 'name': name, 'floor': floor, 'reason': str(exc)})

    assemblies=build_details(records,frames,levels,added,add,add_prism,issues,assumptions)
    bpy.data.collections.remove(scratch)
    bpy.context.view_layer.update()
    checks = checks_for(objects, .25)
    if not all(c['passed'] for c in checks):
        raise ValueError('Ventilation meshes failed dimensional/topology checks')
    all_elements = audit['elements'] + added
    all_checks = audit['checks'] + checks
    counts = dict(Counter(e['category'] for e in all_elements))
    ventilation = {'frames': frames, 'elements': added, 'checks': checks, 'issues': issues, 'excluded': excluded,
                   'source_manifest': manifest, 'base_model_sha256': base_hash,
                   'scope': 'Outer geometry for overall viewing; no fabrication or airflow verification',
                   'complete': False, 'overview_complete':not issues,
                   'assemblies':assemblies,'assumptions':assumptions,
                   'counts': dict(Counter(e['category'] for e in added))}
    audit.update(elements=all_elements, checks=all_checks, counts=counts)
    audit['ventilation'] = ventilation
    audit['verification'].update(check_count=len(all_checks), failure_count=0, coordinate_checks_passed=True,
                                 ventilation_added=True, ventilation_complete=False)
    audit['unresolved'].append({'part':'Ventilation','reason':'Overview envelopes with explicit mounting/product assumptions; no fabrication, airflow or coordinated-penetration approval.'})
    pack('Architectural reconstruction audit.json', audit)
    pack('AutoCAD combined source provenance.json', combined)
    pack('Ventilation reconstruction audit.json', ventilation)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix('.report.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    if digest(base) != base_hash:
        raise ValueError('Original architectural BLEND changed')
    print(json.dumps({'blend': str(target), 'added': len(added), 'counts': ventilation['counts'],
                      'issues': len(issues), 'excluded': len(excluded), 'coordinate_checks_passed': True}, ensure_ascii=True))


if __name__ == '__main__':
    main()
