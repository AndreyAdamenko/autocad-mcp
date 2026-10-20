"""Interpret selected OV COM records; physical sizes never use symbol bounds."""
import math
import re

from .geometry import bounds, transform, world_geometry
from .ventilation import plain_text, rectangular_body, section_from_text, segment_distance


def column_frames(records, regions):
    roots = [r for r in records if r['id'].count('/') == 1]
    frames = {}
    for key in ('ground', 'upper', 'roof'):
        x0, x1, y0, y1 = regions[key]
        selected = [r for r in roots if r.get('source_bbox') and
                    x0 <= r['source_bbox'][0][0] <= r['source_bbox'][1][0] <= x1 and
                    y0 <= r['source_bbox'][0][1] <= r['source_bbox'][1][1] <= y1]
        if key != 'roof':
            anchors = [r for r in selected if r.get('layer') == 'Несущие колонны' and r['geometry']['kind'] == 'block']
            centres = [transform(r['world_matrix'], r['geometry']['insertion']) for r in anchors]
            origin = [min(p[i] for p in centres) for i in (0, 1)]
            spans = [max(p[i] for p in centres) - origin[i] for i in (0, 1)]
        else:
            vertical, horizontal = [], []
            for r in selected:
                if r.get('layer') != 'Оси' or r['geometry']['kind'] != 'line':
                    continue
                a, b = world_geometry(r)[0]
                if abs(a[0] - b[0]) < .01 and abs(a[1] - b[1]) > 10000:
                    vertical.append(r)
                elif abs(a[1] - b[1]) < .01 and abs(a[0] - b[0]) > 20000:
                    horizontal.append(r)
            xs = [world_geometry(r)[0][0][0] for r in vertical]
            ys = [world_geometry(r)[0][0][1] for r in horizontal]
            origin, spans = [min(xs), min(ys)], [max(xs) - min(xs), max(ys) - min(ys)]
            anchors = vertical + horizontal
        if max(abs(a - b) for a, b in zip(spans, (30000, 12900))) > .25:
            raise ValueError('OV axes contradict this building: ' + key)
        frames[key] = {'origin_mm': origin, 'spans_mm': spans, 'source_ids': [r['id'] for r in anchors]}
    return frames


def property_value(record, name):
    return next((p['value'] for p in record['geometry'].get('dynamic_properties', []) if p['name'] == name), None)


def local_point(point, frame):
    return [point[i] - frame['origin_mm'][i] for i in (0, 1)] + [0.0]


def inside_plan(record, frame, margin=1600):
    box = record.get('source_bbox')
    if not box:
        return False
    center = [sum(box[j][i] for j in (0, 1)) / 2 for i in (0, 1)]
    point = local_point(center, frame)
    return -margin < point[0] < 30000 + margin and -margin < point[1] < 12900 + margin


def visible_descendants(records, root):
    by_id = {r['id']: r for r in records}
    selected = []
    for r in records:
        if not r['id'].startswith(root['id'] + '/') or r.get('visible') is False:
            continue
        parts = r['id'].split('/')
        if any(by_id.get('/'.join(parts[:i]), {}).get('visible') is False for i in range(2, len(parts))):
            continue
        selected.append(r)
    return selected


def block_body(records, root, frame, round_section=False):
    children = visible_descendants(records, root)
    line_records = [r for r in children if r['geometry']['kind'] == 'line']
    lines = [world_geometry(r)[0] for r in line_records]
    if round_section:
        visibility = str(property_value(root, 'Видимость') or '')
        section = section_from_text(visibility)
        if not section or section['shape'] != 'round':
            raise ValueError('Round duct has no verified diameter visibility parameter')
        width = section['diameter_mm']
    else:
        width = property_value(root, 'Расстояние1')
        section = None
        if not isinstance(width, (int, float)):
            raise ValueError('Rectangular duct has no width parameter')
    polygon, measured_width = rectangular_body(lines, minimum_length=1, expected_width=width)
    source_ids = []
    for r, points in zip(line_records, lines):
        if all(any(math.dist(p, q) <= .001 for q in polygon) for p in points):
            source_ids.append(r['id'])
    polygon = [local_point(p, frame) for p in polygon]
    path = [[(polygon[0][i] + polygon[3][i]) / 2 for i in range(3)],
            [(polygon[1][i] + polygon[2][i]) / 2 for i in range(3)]]
    return {'polygon': polygon, 'path': path, 'width_mm': measured_width,
            'section': section, 'source_ids': source_ids}


def callouts(records, frame, duct_only=False):
    result = []
    for r in records:
        g = r['geometry']
        if r['id'].count('/') != 1 or not g.get('leader_lines'):
            continue
        text = plain_text(g.get('text', ''))
        section = section_from_text(text)
        if not section:
            continue
        if duct_only and any(s in text.lower() for s in ('диффузор', 'решет', 'решёт', 'раг', 'сетка', 'клап', 'дкс', 'зонт')):
            continue
        for line in g['leader_lines']:
            point = local_point(transform(r['world_matrix'], line['vertices'][0]), frame)
            if -2500 < point[0] < 32500 and -2000 < point[1] < 16000:
                result.append({'id': r['id'], 'point': point, 'section': section, 'text': text})
    return result


def section_for_body(body, labels):
    candidates = []
    for label in labels:
        section = label['section']
        if section['shape'] != 'rectangular' or abs(section['width_mm'] - body['width_mm']) > .25:
            continue
        distance, position = segment_distance(label['point'], *body['path'])
        candidates.append((distance, label))
    if not candidates:
        raise ValueError('Duct height is not established by a same-width size callout')
    distance, selected = min(candidates, key=lambda item: item[0])
    return selected['section'], selected['id'], distance


def inferred_round_system(point):
    # This is a project selection rule; source callouts must verify each cluster.
    if point[0] < 4000:
        return 'В2'
    if point[0] > 16000:
        return 'В5'
    return 'В3' if point[1] > 5000 else 'В4'
